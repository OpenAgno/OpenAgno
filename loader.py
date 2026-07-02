"""
Loader - Carga dinamica del workspace y construye objetos Agno.

Lee archivos YAML/MD del workspace/ y construye:
- Agente principal con tools, instrucciones y MCP
- Sub-agentes desde workspace/agents/*.yaml
- Teams multi-agente desde workspace/agents/teams.yaml
- Schedules desde workspace/schedules.yaml
- Knowledge base con PgVector/Supabase
- Auto-ingesta de documentos y URLs
- Configuracion de canales (WhatsApp, Slack, Telegram, Web)
- Integraciones declarativas en workspace/integrations/*/integration.yaml + config.env
- Modelos AWS Bedrock (AwsBedrock + Claude via Bedrock) (F6)
- WorkspaceTools y SchedulerTools para autonomia del agente (F6)
- Auto-consciencia del agente via self_knowledge.md (F7)
- GithubTools builtin de Agno (F7)
"""
import copy
import os
import re
import yaml
from pathlib import Path
from copy import deepcopy
from typing import Any, Optional, Union

from dotenv import load_dotenv

from agno.agent import Agent
from agno.team import Team
from agno.team.mode import TeamMode
from agno.db.postgres import PostgresDb
from agno.db.sqlite import SqliteDb
from agno.knowledge.knowledge import Knowledge
from agno.knowledge.embedder.openai import OpenAIEmbedder
from agno.vectordb.pgvector import PgVector, SearchType
from agno.memory import MemoryManager
from agno.models.message import Message
from agno.tools.mcp import MCPTools, SSEClientParams, StreamableHTTPClientParams
from mcp import StdioServerParameters
from agno.tools.duckduckgo import DuckDuckGoTools
from agno.tools.crawl4ai import Crawl4aiTools
from agno.tools.reasoning import ReasoningTools
from agno.utils.log import logger

load_dotenv()

WORKSPACE_DIR = Path(os.getenv("AGNOBOT_WORKSPACE", "workspace"))

# Raiz del repo OpenAgno (directorio que contiene gateway.py y loader.py)
_OPENAGNO_REPO_ROOT = Path(__file__).resolve().parent

ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")
HISTORY_SANITIZE_PROVIDERS = {"anthropic", "aws_bedrock", "aws_bedrock_claude"}


def _resolve_env(value: str) -> str:
	"""Resuelve referencias ${VAR} en valores de configuracion."""
	if not isinstance(value, str):
		return value

	def replacer(match: re.Match[str]) -> str:
		return os.getenv(match.group(1), "")

	return ENV_VAR_PATTERN.sub(replacer, value)


def _resolve_value(value: Any) -> Any:
	"""Resuelve referencias ${VAR} en strings, dicts y listas."""
	if isinstance(value, str):
		return _resolve_env(value)
	if isinstance(value, dict):
		return {k: _resolve_value(v) for k, v in value.items()}
	if isinstance(value, list):
		return [_resolve_value(v) for v in value]
	return value


def _resolve_config(config: dict[str, Any]) -> dict[str, Any]:
	"""Resuelve todas las referencias ${VAR} en un dict."""
	return {k: _resolve_value(v) for k, v in config.items()}


def load_yaml(filename: str, workspace_dir: Path | None = None) -> dict[str, Any]:
	"""Carga un archivo YAML desde el directorio de workspace (o el default)."""
	base = workspace_dir or WORKSPACE_DIR
	path = base / filename
	if not path.exists():
		logger.warning(f"Archivo no encontrado: {path}")
		return {}
	with open(path, "r", encoding="utf-8") as f:
		return yaml.safe_load(f) or {}


def load_integration_manifests(ws: Path) -> list[tuple[Path, dict[str, Any]]]:
	"""
	Lee workspace/integrations/<id>/integration.yaml.
	Retorna lista (directorio_integracion, manifest_dict).
	"""
	integ_root = ws / "integrations"
	if not integ_root.is_dir():
		return []
	out: list[tuple[Path, dict[str, Any]]] = []
	for sub in sorted(integ_root.iterdir()):
		if not sub.is_dir() or sub.name.startswith("."):
			continue
		manifest_path = sub / "integration.yaml"
		if not manifest_path.is_file():
			continue
		try:
			raw = manifest_path.read_text(encoding="utf-8")
			data = yaml.safe_load(raw) or {}
		except yaml.YAMLError as e:
			logger.warning(f"integrations/{sub.name}/integration.yaml YAML invalido: {e}")
			continue
		if not isinstance(data, dict):
			continue
		out.append((sub, data))
	return out


def preload_integration_environments(manifests: list[tuple[Path, dict[str, Any]]]) -> None:
	"""Carga archivos env de cada integracion habilitada (override=False respecto al .env raiz)."""
	for integ_dir, data in manifests:
		if not data.get("enabled", True):
			continue
		env_files = data.get("env_files")
		if env_files is None:
			if "env_file" in data and not data["env_file"]:
				env_files = []
			else:
				single = data.get("env_file", "config.env")
				env_files = [single] if single else []
		if not isinstance(env_files, list):
			env_files = [env_files]
		for rel in env_files:
			if not rel or not isinstance(rel, str):
				continue
			fp = integ_dir / rel
			if fp.is_file():
				load_dotenv(fp, override=False)
				iid = data.get("id", integ_dir.name)
				logger.info(f"Integracion '{iid}': variables desde {fp.name}")


def _enable_optional_in_merged(
	optional: list[dict[str, Any]],
	by_name: dict[str, dict[str, Any]],
	name: str,
	extra_config: dict[str, Any],
) -> None:
	if name in by_name:
		entry = by_name[name]
		entry["enabled"] = True
		cfg = entry.setdefault("config", {})
		if isinstance(cfg, dict) and isinstance(extra_config, dict):
			cfg.update(extra_config)
	else:
		new_e: dict[str, Any] = {"name": name, "enabled": True, "config": dict(extra_config) if extra_config else {}}
		optional.append(new_e)
		by_name[name] = new_e


def merge_tools_config_with_integrations(
	tools_config: dict[str, Any],
	manifests: list[tuple[Path, dict[str, Any]]],
) -> dict[str, Any]:
	"""Fusiona tools.yaml con optional_tool / optional_tools declarados en integraciones."""
	merged = copy.deepcopy(tools_config)
	optional = merged.setdefault("optional", [])
	by_name: dict[str, dict[str, Any]] = {}
	for od in optional:
		n = od.get("name")
		if isinstance(n, str):
			by_name[n] = od

	for _integ_dir, data in manifests:
		if not data.get("enabled", True):
			continue
		ot = data.get("optional_tool")
		if isinstance(ot, str):
			_enable_optional_in_merged(
				optional, by_name, ot, data.get("tool_config") or {}
			)
		ots = data.get("optional_tools")
		if isinstance(ots, list):
			for item in ots:
				if isinstance(item, str):
					_enable_optional_in_merged(optional, by_name, item, {})
				elif isinstance(item, dict):
					nm = item.get("name")
					if isinstance(nm, str):
						_enable_optional_in_merged(
							optional, by_name, nm, item.get("config") or {}
						)
	return merged


def merge_mcp_config_with_integrations(
	mcp_config: dict[str, Any],
	manifests: list[tuple[Path, dict[str, Any]]],
) -> dict[str, Any]:
	"""Anade o sobrescribe entradas de mcp.yaml desde integraciones."""
	merged = copy.deepcopy(mcp_config)
	servers = merged.setdefault("servers", [])
	by_name: dict[str, dict[str, Any]] = {}
	for s in servers:
		n = s.get("name")
		if isinstance(n, str):
			by_name[n] = s

	for _integ_dir, data in manifests:
		if not data.get("enabled", True):
			continue
		blocks: list[dict[str, Any]] = []
		m = data.get("mcp")
		if isinstance(m, dict):
			blocks.append(m)
		ms = data.get("mcp_servers")
		if isinstance(ms, list):
			blocks.extend(b for b in ms if isinstance(b, dict))
		for raw in blocks:
			block = _resolve_config(copy.deepcopy(raw))
			sname = block.get("name") or data.get("id")
			if not isinstance(sname, str) or not sname:
				sname = "integration-mcp"
			block["name"] = sname
			block.setdefault("enabled", True)
			if sname in by_name:
				by_name[sname].update(block)
			else:
				servers.append(block)
				by_name[sname] = block
	return merged


def load_instructions(workspace_dir: Path | None = None) -> list[str]:
	"""Carga instrucciones desde instructions.md."""
	base = workspace_dir or WORKSPACE_DIR
	path = base / "instructions.md"
	if not path.exists():
		return ["Eres un asistente personal multimodal."]
	with open(path, "r", encoding="utf-8") as f:
		content = f.read().strip()
	return [content]


def build_db_url(db_config: dict[str, Any]) -> str:
	"""Construye la URL de conexion segun el tipo de DB."""
	db_type = db_config.get("type", "local")

	if db_type == "sqlite":
		return "sqlite:///tmp/agnobot.db"

	host = os.getenv("DB_HOST", "localhost")
	port = os.getenv("DB_PORT", "5532")
	user = os.getenv("DB_USER", "ai")
	password = os.getenv("DB_PASSWORD", "ai")
	name = os.getenv("DB_NAME", "ai")
	sslmode = os.getenv("DB_SSLMODE", "prefer" if db_type == "local" else "require")

	return (
		f"postgresql+psycopg://{user}:{password}"
		f"@{host}:{port}/{name}?sslmode={sslmode}"
	)


def build_db(db_url: str, db_config: dict[str, Any]) -> Union[PostgresDb, SqliteDb]:
	"""Construye el objeto de base de datos."""
	if db_url.startswith("sqlite"):
		return SqliteDb(db_file="tmp/agnobot.db")
	return PostgresDb(
		db_url=db_url,
		id="agnobot_db",
		knowledge_table=db_config.get("knowledge_table", "agnobot_knowledge_contents"),
	)


def build_knowledge(
	db_url: str,
	db: Union[PostgresDb, SqliteDb],
	vector_config: dict[str, Any],
	db_config: dict[str, Any],
) -> Optional[Knowledge]:
	"""Construye la Knowledge base con PgVector."""
	if db_url.startswith("sqlite"):
		logger.warning("SQLite no soporta PgVector. Knowledge deshabilitada.")
		return None

	search_type_map: dict[str, SearchType] = {
		"hybrid": SearchType.hybrid,
		"vector": SearchType.vector,
		"keyword": SearchType.keyword,
	}

	return Knowledge(
		name=vector_config.get("name"),
		vector_db=PgVector(
			table_name=db_config.get("vector_table", "agnobot_knowledge_vectors"),
			db_url=db_url,
			search_type=search_type_map.get(
				vector_config.get("search_type", "hybrid"),
				SearchType.hybrid,
			),
			embedder=OpenAIEmbedder(
				id=vector_config.get("embedder", "text-embedding-3-small")
			),
		),
		contents_db=db,
		max_results=vector_config.get("max_results", 5),
		isolate_vector_search=vector_config.get("isolate_vector_search", False),
	)


BUILTIN_TOOL_MAP: dict[str, Any] = {
	"duckduckgo": lambda cfg: DuckDuckGoTools(**cfg),
	"crawl4ai": lambda cfg: Crawl4aiTools(**cfg),
	"reasoning": lambda cfg: ReasoningTools(**cfg),
}


def build_tools(
	tools_config: dict[str, Any],
	*,
	workspace_dir: Path | None = None,
	tenant_slug: str | None = None,
	on_reload: Any = None,
) -> list[Any]:
	"""Construye la lista de tools segun tools.yaml.

	`workspace_dir`, `tenant_slug` y `on_reload` se propagan a los toolkits
	que dependen del workspace del tenant (hoy: WorkspaceTools). Si no se
	pasan, los toolkits caen a su comportamiento legacy global.
	"""
	tools: list[Any] = []

	for tool_def in tools_config.get("builtin", []):
		if not tool_def.get("enabled", True):
			continue
		name = tool_def["name"]
		config = _resolve_config(tool_def.get("config", {}))
		factory = BUILTIN_TOOL_MAP.get(name)
		if factory is not None:
			tools.append(factory(config))
		else:
			logger.warning(f"Tool builtin desconocido: {name}")

	for tool_def in tools_config.get("optional", []):
		if not tool_def.get("enabled", False):
			continue
		name = tool_def["name"]
		config = _resolve_config(tool_def.get("config", {}))

		match name:
			case "email":
				from agno.tools.email import EmailTools
				if config.get("sender_email") and config.get("sender_passkey"):
					tools.append(EmailTools(**config))
			case "tavily":
				from agno.tools.tavily import TavilyTools
				tools.append(TavilyTools())
			case "spotify":
				from agno.tools.spotify import SpotifyTools
				tools.append(SpotifyTools())
			case "shell":
				from agno.tools.shell import ShellTools
				logger.warning("ShellTools activado - riesgo de seguridad")
				raw_base = config.get("base_dir")
				base_path: Path
				if isinstance(raw_base, str) and raw_base.strip():
					base_path = Path(raw_base).expanduser().resolve()
				else:
					base_path = _OPENAGNO_REPO_ROOT
				tools.append(ShellTools(base_dir=base_path))
			case "workspace":
				from tools.workspace_tools import WorkspaceTools
				tools.append(WorkspaceTools(
					workspace_dir=workspace_dir,
					tenant_slug=tenant_slug,
					on_reload=on_reload,
				))
				logger.info(
					f"WorkspaceTools activado — auto-configuracion habilitada"
					f"{' (tenant=' + tenant_slug + ')' if tenant_slug else ''}"
				)
			case "scheduler_mgmt":
				from tools.scheduler_tools import SchedulerTools
				tools.append(SchedulerTools())
				logger.info("SchedulerTools activado — gestion de crons via API REST")
			case "audio":
				from tools.audio_tools import AudioTools
				audio_cfg = _resolve_config(config)
				tools.append(AudioTools(
					stt_model=audio_cfg.get("stt_model", "whisper-1"),
					tts_model=audio_cfg.get("tts_model", "tts-1"),
					tts_voice=audio_cfg.get("tts_voice", "nova"),
					tts_enabled=audio_cfg.get("tts_enabled", False),
					auto_transcribe=audio_cfg.get("auto_transcribe", True),
				))
				logger.info(f"AudioTools activado — STT: {audio_cfg.get('stt_model', 'whisper-1')}, TTS: {'ON' if audio_cfg.get('tts_enabled') else 'OFF'}")
			case "github":
				try:
					from agno.tools.github import GithubTools
					# El toolkit completo expone ~50 funciones y satura el limite de
					# 128 tools por request de OpenAI cuando convive con MCP servers
					# (ej. Linear) y WorkspaceTools. Por defecto exponemos el nucleo
					# de lectura/busqueda + issues/PRs; `config.include_tools` en
					# tools.yaml permite ampliar la lista por tenant.
					default_github_tools = [
						"list_repositories",
						"get_repository",
						"search_repositories",
						"search_code",
						"get_file_content",
						"get_directory_content",
						"list_branches",
						"list_issues",
						"get_issue",
						"create_issue",
						"get_pull_requests",
						"get_pull_request",
						"search_issues_and_prs",
					]
					include_tools = config.get("include_tools") or default_github_tools
					tools.append(GithubTools(include_tools=include_tools))
					logger.info(
						f"GithubTools activado ({len(include_tools)} funciones) — usa GITHUB_ACCESS_TOKEN del entorno"
					)
				except ImportError:
					logger.warning("GithubTools no disponible — instalar PyGithub>=2.0")
				except ValueError as e:
					# Sin GITHUB_ACCESS_TOKEN configurado GithubTools lanza ValueError.
					# No debe tumbar la carga del workspace: se omite el tool y el
					# agente queda funcional hasta que el operador ponga la key.
					logger.warning(f"GithubTools omitido — {e} (definir GITHUB_ACCESS_TOKEN y reiniciar)")
			case "yfinance":
				try:
					from agno.tools.yfinance import YFinanceTools
					tools.append(YFinanceTools(**config))
					logger.info("YFinanceTools activado")
				except ImportError:
					logger.warning("YFinanceTools no disponible — instalar yfinance>=0.2.0")
			case "wikipedia":
				try:
					from agno.tools.wikipedia import WikipediaTools
					tools.append(WikipediaTools(**config))
					logger.info("WikipediaTools activado")
				except ImportError:
					logger.warning("WikipediaTools no disponible — instalar wikipedia>=1.4.0")
			case "arxiv":
				try:
					from agno.tools.arxiv import ArxivTools
					tools.append(ArxivTools(**config))
					logger.info("ArxivTools activado")
				except ImportError:
					logger.warning("ArxivTools no disponible — instalar arxiv>=2.0.0")
			case "calculator":
				try:
					from agno.tools.calculator import CalculatorTools
					tools.append(CalculatorTools(**config))
					logger.info("CalculatorTools activado")
				except ImportError:
					logger.warning("CalculatorTools no disponible")
			case "file_tools":
				try:
					from agno.tools.file import FileTools
					tools.append(FileTools(**config))
					logger.info("FileTools activado")
				except ImportError:
					logger.warning("FileTools no disponible")
			case "python_tools":
				try:
					from agno.tools.python import PythonTools
					tools.append(PythonTools(**config))
					logger.warning("PythonTools activado — riesgo de seguridad")
				except ImportError:
					logger.warning("PythonTools no disponible")
			case _:
				logger.warning(f"Tool opcional desconocido: {name}")

	return tools


def build_mcp_tools(mcp_config: dict[str, Any]) -> list[MCPTools]:
	"""Construye MCPTools segun mcp.yaml. Soporta streamable-http, sse y stdio."""
	mcp_tools: list[MCPTools] = []

	for server in mcp_config.get("servers", []):
		if not server.get("enabled", False):
			continue

		transport = server.get("transport", "streamable-http")
		name = server.get("name", "mcp-server")

		if transport in ("streamable-http", "sse"):
			url = _resolve_env(server.get("url", ""))
			headers = _resolve_config(server.get("headers", {}))
			if url:
				try:
					if transport == "sse":
						server_params = SSEClientParams(url=url, headers=headers or None)
					else:
						server_params = StreamableHTTPClientParams(url=url, headers=headers or None)
					# refresh_connection=True: los bundles de tenant se cargan dentro
					# de una request HTTP y la sesion MCP queda atada al task scope de
					# esa request; sin refresh, el siguiente run falla con "Cancelled
					# via cancel scope". Con refresh cada run reconecta limpio.
					mcp_tools.append(
						MCPTools(
							transport=transport,
							server_params=server_params,
							refresh_connection=True,
						)
					)
					logger.info(f"MCP '{name}' ({transport}): {url}")
				except Exception as e:
					logger.warning(f"MCP '{name}' fallo al inicializar: {e}")

		elif transport == "stdio":
			command = _resolve_env(server.get("command", ""))
			raw_args = server.get("args", [])
			args = _resolve_value(raw_args) if isinstance(raw_args, list) else []
			env = _resolve_config(server.get("env", {}))
			if not command:
				continue

			try:
				server_params = StdioServerParameters(
					command=command,
					args=args,
					env=env or None,
				)
				mcp_tool = MCPTools(
					transport="stdio",
					server_params=server_params,
				)
				mcp_tools.append(mcp_tool)
				logger.info(f"MCP '{name}' (stdio): {command} {' '.join(args[:3])}".rstrip())
			except Exception as e:
				logger.warning(f"MCP '{name}' fallo al inicializar: {e}")

	return mcp_tools


def detect_model_provider(model: Any) -> str:
	"""Infiere el provider a partir del tipo de modelo Agno activo."""
	module_name = getattr(type(model), "__module__", "")
	class_name = getattr(type(model), "__name__", "").lower()

	if ".models.google." in module_name:
		return "google"
	if ".models.openai." in module_name:
		return "openai"
	if ".models.anthropic." in module_name:
		return "anthropic"
	if ".models.aws." in module_name:
		return "aws_bedrock_claude" if "claude" in class_name else "aws_bedrock"

	return module_name.rsplit(".", 1)[-1] if module_name else "unknown"


def _is_valid_tool_call_id(value: Any) -> bool:
	return isinstance(value, str) and bool(value.strip())


def sanitize_history_for_provider(messages: list[Message], provider: str) -> list[Message]:
	"""Limpia tool calls/resultados incompatibles antes de enviarlos al provider."""
	if provider not in HISTORY_SANITIZE_PROVIDERS:
		return messages

	sanitized: list[Message] = []
	valid_tool_call_ids: set[str] = set()

	for message in messages:
		if message.role == "assistant" and message.tool_calls:
			message_copy = deepcopy(message)
			filtered_tool_calls = []
			for tool_call in message.tool_calls:
				tool_call_id = tool_call.get("id") if isinstance(tool_call, dict) else None
				if _is_valid_tool_call_id(tool_call_id):
					filtered_tool_calls.append(tool_call)
					valid_tool_call_ids.add(tool_call_id)

			message_copy.tool_calls = filtered_tool_calls or None
			has_content = bool(message_copy.get_content_string().strip())
			if message_copy.tool_calls or has_content:
				sanitized.append(message_copy)
			continue

		if message.role == "tool":
			if not _is_valid_tool_call_id(message.tool_call_id):
				continue
			if message.tool_call_id not in valid_tool_call_ids:
				continue

		sanitized.append(deepcopy(message))

	return sanitized


def sanitize_session_history_for_provider(session: Any, provider: str, max_runs: Optional[int] = None) -> int:
	"""Sanea los runs recientes de una sesion cargada desde DB."""
	if provider not in HISTORY_SANITIZE_PROVIDERS or session is None or not getattr(session, "runs", None):
		return 0

	runs = session.runs[-max_runs:] if max_runs else session.runs
	removed_messages = 0

	for run in runs:
		run_messages = getattr(run, "messages", None) or []
		sanitized_messages = sanitize_history_for_provider(run_messages, provider)
		removed_messages += max(0, len(run_messages) - len(sanitized_messages))
		run.messages = sanitized_messages

	return removed_messages


def sanitize_session_history_pre_hook(session: Any, agent: Agent, **_: Any) -> None:
	"""Pre-hook Agno: limpia historial incompatible antes de construir messages."""
	provider = detect_model_provider(agent.model)
	removed = sanitize_session_history_for_provider(
		session=session,
		provider=provider,
		max_runs=agent.num_history_runs,
	)
	if removed > 0:
		logger.warning(
			f"Historial de sesion sanitizado para provider '{provider}': "
			f"{removed} mensaje(s) tool incompatibles removidos"
		)


# Proveedores que NO soportan audio nativo (necesitan transcripcion via Whisper/GPT-4o-mini)
# Gemini Flash y OpenAI GPT-4o+ son multimodal nativos y no necesitan esto.
NON_AUDIO_PROVIDERS = {"aws_bedrock", "aws_bedrock_claude", "anthropic"}

# Patrones de error que indican rate-limit/quota excedida
RATE_LIMIT_PATTERNS = [
	"rate_limit", "rate limit", "quota", "throttl",
	"429", "too many requests", "capacity", "overloaded",
	"resourceexhausted", "throttlingexception", "toomanyrequestsexception",
	"serviceunavailableexception",
]


def is_rate_limit_error(error: Exception) -> bool:
	"""Detecta si un error es por rate-limit/quota."""
	msg = str(error).lower()
	error_type = type(error).__name__.lower()
	return any(p in msg or p in error_type for p in RATE_LIMIT_PATTERNS)


def _build_single_model(
	provider: str,
	model_id: str,
	aws_region: str,
	*,
	api_key: Optional[str] = None,
	aws_access_key_id: Optional[str] = None,
	aws_secret_access_key: Optional[str] = None,
) -> Any:
	"""Construye una instancia de modelo por proveedor.

	Proveedores verificados contra docs.agno.com:
	- google:             from agno.models.google import Gemini
	- openai:             from agno.models.openai import OpenAIChat
	- anthropic:          from agno.models.anthropic import Claude
	- aws_bedrock:        from agno.models.aws import AwsBedrock  (Mistral, Nova)
	- aws_bedrock_claude: from agno.models.aws import Claude       (Anthropic via Bedrock)

	Credenciales BYOK (per-tenant): cuando `api_key` o `aws_*` vienen desde el
	workspace del tenant, se pasan como kwargs a la clase Agno para que ese
	tenant use sus propias keys. Cuando no vienen (por ejemplo el workspace del
	operador), los kwargs son `None` y Agno cae automaticamente al valor de
	`os.environ`, que es como se ha venido usando historicamente.
	"""
	# Solo pasamos el kwarg si tiene valor; None explicito podria sobrescribir
	# los defaults internos de Agno en algunas clases.
	single_key_kwargs: dict[str, Any] = {}
	if api_key:
		single_key_kwargs["api_key"] = api_key

	match provider:
		case "google":
			from agno.models.google import Gemini
			return Gemini(id=model_id, **single_key_kwargs)
		case "openai":
			from agno.models.openai import OpenAIChat
			return OpenAIChat(id=model_id, **single_key_kwargs)
		case "anthropic":
			from agno.models.anthropic import Claude
			return Claude(id=model_id, **single_key_kwargs)
		case "aws_bedrock":
			from agno.models.aws import AwsBedrock
			aws_kwargs: dict[str, Any] = {"aws_region": aws_region}
			if aws_access_key_id:
				aws_kwargs["aws_access_key_id"] = aws_access_key_id
			if aws_secret_access_key:
				aws_kwargs["aws_secret_access_key"] = aws_secret_access_key
			return AwsBedrock(id=model_id, **aws_kwargs)
		case "aws_bedrock_claude":
			from agno.models.aws import Claude as BedrockClaude
			# agno.models.aws.Claude expone `aws_access_key`/`aws_secret_key`
			# (sin sufijo `_id`/`_access`). Mantener el mapeo explicito.
			bedrock_kwargs: dict[str, Any] = {"aws_region": aws_region}
			if aws_access_key_id:
				bedrock_kwargs["aws_access_key"] = aws_access_key_id
			if aws_secret_access_key:
				bedrock_kwargs["aws_secret_key"] = aws_secret_access_key
			return BedrockClaude(id=model_id, **bedrock_kwargs)
		case _:
			raise ValueError(f"Proveedor de modelo no soportado: {provider}")


def build_model(model_config: dict[str, Any]) -> Any:
	"""Construye el modelo con fallback opcional para rate limits.

	`model_config` puede incluir credenciales BYOK del tenant:
	- `api_key`: OpenAI, Anthropic, Google Gemini.
	- `aws_access_key_id`, `aws_secret_access_key`: AWS Bedrock (Claude o base).

	Si no vienen (ej. workspace del operador que confia en el .env del servidor),
	Agno cae automaticamente a los valores de `os.environ`.
	"""
	provider = model_config.get("provider", "google")
	model_id = model_config.get("id", "gemini-2.5-flash")
	aws_region = model_config.get("aws_region", os.getenv("AWS_REGION", "us-east-1"))
	api_key = model_config.get("api_key")
	aws_access_key_id = model_config.get("aws_access_key_id")
	aws_secret_access_key = model_config.get("aws_secret_access_key")

	return _build_single_model(
		provider,
		model_id,
		aws_region,
		api_key=api_key,
		aws_access_key_id=aws_access_key_id,
		aws_secret_access_key=aws_secret_access_key,
	)


def build_fallback_model(
	config: dict[str, Any],
	model_config: Optional[dict[str, Any]] = None,
) -> Optional[Any]:
	"""Construye el modelo fallback si esta configurado.

	Soporta la forma actual documentada en `config.yaml` (`fallback` top-level)
	y la forma legacy `model.fallback` para compatibilidad hacia atras.
	"""
	resolved_model_config = model_config or config.get("model", config)
	if not isinstance(resolved_model_config, dict):
		resolved_model_config = {}

	fallback_config = config.get("fallback", {})
	if not isinstance(fallback_config, dict) or not fallback_config:
		fallback_config = resolved_model_config.get("fallback", {})

	if not isinstance(fallback_config, dict):
		return None

	if fallback_config.get("enabled") is False or not fallback_config.get("id"):
		return None

	provider = resolved_model_config.get("provider", "google")
	aws_region = resolved_model_config.get(
		"aws_region", os.getenv("AWS_REGION", "us-east-1")
	)

	fb_provider = fallback_config.get("provider", provider)
	fb_id = fallback_config["id"]
	fb_region = fallback_config.get("aws_region", aws_region)
	# Credenciales: si el fallback no tiene las suyas, hereda del modelo principal
	# (escenario mas comun: mismo tenant usando el mismo provider con otro modelo).
	fb_api_key = fallback_config.get("api_key") or resolved_model_config.get("api_key")
	fb_aws_access = (
		fallback_config.get("aws_access_key_id")
		or resolved_model_config.get("aws_access_key_id")
	)
	fb_aws_secret = (
		fallback_config.get("aws_secret_access_key")
		or resolved_model_config.get("aws_secret_access_key")
	)

	try:
		fb_model = _build_single_model(
			fb_provider,
			fb_id,
			fb_region,
			api_key=fb_api_key,
			aws_access_key_id=fb_aws_access,
			aws_secret_access_key=fb_aws_secret,
		)
		logger.info(f"Modelo fallback disponible: {fb_provider}/{fb_id}")
		return fb_model
	except Exception as e:
		logger.warning(f"No se pudo construir modelo fallback: {e}")
		return None


TEAM_MODE_MAP: dict[str, TeamMode] = {
	"coordinate": TeamMode.coordinate,
	"route": TeamMode.route,
	"broadcast": TeamMode.broadcast,
	"tasks": TeamMode.tasks,
}


def build_sub_agents(
	db: Union[PostgresDb, SqliteDb],
	knowledge: Optional[Knowledge],
	workspace_dir: Path | None = None,
) -> list[Agent]:
	"""
	Carga sub-agentes desde workspace/agents/*.yaml.
	Excluye teams.yaml (procesado por build_teams).
	"""
	base = workspace_dir or WORKSPACE_DIR
	agents: list[Agent] = []
	agents_dir = base / "agents"
	if not agents_dir.exists():
		return agents

	excluded = {"teams.yaml"}

	for yaml_file in sorted(agents_dir.glob("*.yaml")):
		if yaml_file.name in excluded:
			continue

		try:
			data = yaml.safe_load(yaml_file.read_text(encoding="utf-8")) or {}
		except yaml.YAMLError as e:
			logger.warning(f"YAML invalido en {yaml_file.name}: {e}")
			continue

		agent_def = data.get("agent", {})
		if not agent_def:
			logger.warning(f"Sin definicion 'agent' en {yaml_file.name}")
			continue

		try:
			model_cfg = agent_def.get("model", {"provider": "google", "id": "gemini-2.5-flash"})
			model = build_model(model_cfg)

			agent_tools: list[Union[DuckDuckGoTools, Crawl4aiTools, ReasoningTools]] = []
			for tool_name in agent_def.get("tools", []):
				factory = BUILTIN_TOOL_MAP.get(tool_name)
				if factory is not None:
					agent_tools.append(factory({}))
				else:
					logger.warning(f"Tool '{tool_name}' no reconocido en {yaml_file.name}")

			config = agent_def.get("config", {})

			sub_memory_manager = None
			if config.get("enable_agentic_memory", False):
				sub_memory_manager = MemoryManager(model=model, db=db)

			agent = Agent(
				name=agent_def.get("name", "Sub Agent"),
				id=agent_def.get("id", yaml_file.stem),
				role=agent_def.get("role", ""),
				model=model,
				db=db,
				knowledge=knowledge,
				search_knowledge=knowledge is not None,
				tools=agent_tools,
				instructions=agent_def.get("instructions", []),
				memory_manager=sub_memory_manager,
				enable_agentic_memory=config.get("enable_agentic_memory", False),
				tool_call_limit=config.get("tool_call_limit", 3),
				add_datetime_to_context=config.get("add_datetime_to_context", True),
				markdown=config.get("markdown", True),
				pre_hooks=[sanitize_session_history_pre_hook],
			)
			agents.append(agent)
			logger.info(f"Sub-agente cargado: {agent.name} ({agent.id})")

		except Exception as e:
			# F7 — 7.8: Logs mejorados para sub-agentes fallidos
			logger.error(
				f"Sub-agente '{yaml_file.stem}' NO cargado: {e}\n"
				f"  Provider: {agent_def.get('model', {}).get('provider', '?')}\n"
				f"  Tools: {agent_def.get('tools', [])}\n"
				f"  Accion: Verificar provider y tools contra workspace/self_knowledge.md"
			)
			continue

	return agents


def build_teams(
	all_agents: list[Agent],
	db: Union[PostgresDb, SqliteDb],
	workspace_dir: Path | None = None,
) -> list[Team]:
	"""
	Carga Teams desde workspace/agents/teams.yaml.
	Resuelve miembros por ID contra la lista de agentes disponibles.
	"""
	teams_data = load_yaml("agents/teams.yaml", workspace_dir=workspace_dir)
	teams_list = teams_data.get("teams", [])
	if not teams_list:
		return []

	agent_index: dict[str, Agent] = {a.id: a for a in all_agents if a.id}

	teams: list[Team] = []

	for team_def in teams_list:
		if isinstance(team_def, dict) and team_def.get("enabled") is False:
			logger.info(f"Team deshabilitado omitido: {team_def.get('id', team_def.get('name', '?'))}")
			continue
		team_name = team_def.get("name", "Unnamed Team")

		member_ids: list[str] = team_def.get("members", [])
		members: list[Agent] = []
		for mid in member_ids:
			agent = agent_index.get(mid)
			if agent is not None:
				members.append(agent)
			else:
				logger.warning(
					f"Team '{team_name}': miembro '{mid}' no encontrado. "
					f"Disponibles: {list(agent_index.keys())}"
				)

		if len(members) < 2:
			logger.warning(
				f"Team '{team_name}' necesita al menos 2 miembros, "
				f"tiene {len(members)}. Omitido."
			)
			continue

		model_cfg = team_def.get("model", {"provider": "google", "id": "gemini-2.5-flash"})
		try:
			model = build_model(model_cfg)
		except ValueError as e:
			logger.warning(f"Modelo invalido en team '{team_name}': {e}")
			continue

		mode_str = team_def.get("mode", "coordinate")
		mode = TEAM_MODE_MAP.get(mode_str)
		if mode is None:
			logger.warning(
				f"Team '{team_name}': modo '{mode_str}' no valido. "
				f"Opciones: {list(TEAM_MODE_MAP.keys())}. Usando 'coordinate'."
			)
			mode = TeamMode.coordinate

		team = Team(
			name=team_name,
			id=team_def.get("id", team_name.lower().replace(" ", "-")),
			mode=mode,
			members=members,
			model=model,
			db=db,
			instructions=team_def.get("instructions", []),
			markdown=True,
			enable_agentic_memory=team_def.get("enable_agentic_memory", False),
			pre_hooks=[sanitize_session_history_pre_hook],
		)
		teams.append(team)
		logger.info(
			f"Team cargado: {team.name} ({team.id}) — "
			f"modo={mode_str}, miembros={[m.id for m in members]}"
		)

	return teams


def build_schedules(workspace_dir: Path | None = None) -> list[dict[str, str]]:
	"""Carga schedules desde workspace/schedules.yaml."""
	schedules_config = load_yaml("schedules.yaml", workspace_dir=workspace_dir)
	schedules: list[dict[str, str]] = []

	for sched in schedules_config.get("schedules", []):
		if not sched.get("enabled", True):
			continue

		name = sched.get("name", "Sin nombre")
		cron = sched.get("cron", "")
		message = sched.get("message", "")

		if not cron or not message:
			logger.warning(f"Schedule '{name}' incompleto (falta cron o message). Omitido.")
			continue

		schedules.append({
			"name": name,
			"agent_id": sched.get("agent_id", "agnobot-main"),
			"cron": cron,
			"timezone": sched.get("timezone", "America/Guayaquil"),
			"message": message,
			"user_id": sched.get("user_id", "scheduler"),
		})
		logger.info(f"Schedule registrado: '{name}' ({cron})")

	return schedules


def load_knowledge_urls(workspace_dir: Path | None = None) -> list[dict[str, str]]:
	"""Carga URLs para ingesta desde workspace/knowledge/urls.yaml."""
	urls_config = load_yaml("knowledge/urls.yaml", workspace_dir=workspace_dir)
	return urls_config.get("urls", [])


def get_knowledge_docs_paths(workspace_dir: Path | None = None) -> list[Path]:
	"""Retorna lista de archivos soportados en workspace/knowledge/docs/."""
	base = workspace_dir or WORKSPACE_DIR
	docs_dir = base / "knowledge" / "docs"
	if not docs_dir.exists():
		return []

	supported_extensions = {".pdf", ".md", ".txt", ".docx", ".csv", ".json"}
	paths: list[Path] = []
	for f in sorted(docs_dir.iterdir()):
		if f.is_file() and f.suffix.lower() in supported_extensions:
			paths.append(f)

	return paths


def load_workspace() -> dict[str, Any]:
	"""Carga el workspace por defecto (WORKSPACE_DIR). Wrapper de compatibilidad."""
	return load_workspace_from_dir(WORKSPACE_DIR)


def load_workspace_from_dir(
	workspace_dir: Path,
	*,
	tenant_slug: str | None = None,
	on_reload: Any = None,
) -> dict[str, Any]:
	"""
	Carga completa del workspace desde un directorio arbitrario.

	Retorna un dict con todos los objetos necesarios para construir el AgentOS.
	Usado por el tenant_loader para aislar workspaces per-tenant sin modificar
	el estado global del proceso.

	`tenant_slug` y `on_reload` se propagan a los toolkits que dependen del
	tenant (hoy WorkspaceTools) para que sus mutaciones invaliden unicamente
	la cache del tenant correspondiente sin reiniciar el daemon global.
	"""
	workspace_dir = Path(workspace_dir)
	config = load_yaml("config.yaml", workspace_dir=workspace_dir)
	integration_manifests = load_integration_manifests(workspace_dir)
	preload_integration_environments(integration_manifests)
	tools_config = merge_tools_config_with_integrations(
		load_yaml("tools.yaml", workspace_dir=workspace_dir), integration_manifests
	)
	mcp_config = merge_mcp_config_with_integrations(
		load_yaml("mcp.yaml", workspace_dir=workspace_dir), integration_manifests
	)

	db_config = config.get("database", {})
	db_url = build_db_url(db_config)
	db = build_db(db_url, db_config)

	vector_config = config.get("vector", {})
	knowledge = build_knowledge(db_url, db, vector_config, db_config)

	tools = build_tools(
		tools_config,
		workspace_dir=workspace_dir,
		tenant_slug=tenant_slug,
		on_reload=on_reload,
	)
	mcp_tools = build_mcp_tools(mcp_config)
	tools.extend(mcp_tools)

	instructions = load_instructions(workspace_dir=workspace_dir)

	# F7 — 7.4.2: Inyectar self_knowledge.md para auto-consciencia del agente
	self_knowledge_path = workspace_dir / "self_knowledge.md"
	if self_knowledge_path.exists():
		self_knowledge = self_knowledge_path.read_text(encoding="utf-8")
		instructions.append(self_knowledge)
		logger.info("Self-knowledge cargado para auto-consciencia")

	model_config = config.get("model", {})
	model = build_model(model_config)
	fallback_model = build_fallback_model(config, model_config=model_config)

	# Auto-cargar AudioTools si el modelo no soporta audio nativo
	# y hay configuracion de audio habilitada
	audio_config = config.get("audio", {})
	model_provider = model_config.get("provider", "google")
	_has_audio_tool = any(
		getattr(t, "name", "") == "audio_tools" for t in tools
	)
	if not _has_audio_tool and (
		audio_config.get("auto_transcribe", False)
		or audio_config.get("tts_enabled", False)
	):
		from tools.audio_tools import AudioTools
		tools.append(AudioTools(
			stt_model=audio_config.get("stt_model", "whisper-1"),
			tts_model=audio_config.get("tts_model", "tts-1"),
			tts_voice=audio_config.get("tts_voice", "nova"),
			tts_enabled=audio_config.get("tts_enabled", False),
			auto_transcribe=audio_config.get("auto_transcribe", True),
		))
		logger.info(
			f"AudioTools auto-cargado para proveedor '{model_provider}' "
			f"(STT: {audio_config.get('stt_model', 'whisper-1')}, "
			f"TTS: {'ON' if audio_config.get('tts_enabled') else 'OFF'})"
		)

	mem_config = config.get("memory", {})
	agent_config = config.get("agent", {})

	memory_manager = None
	if mem_config.get("enable_agentic_memory", True):
		memory_manager = MemoryManager(model=model, db=db)

	main_agent = Agent(
		name=agent_config.get("name", "AgnoBot"),
		id=agent_config.get("id", "agnobot-main"),
		description=agent_config.get("description", "Asistente personal multimodal"),
		model=model,
		db=db,
		knowledge=knowledge,
		search_knowledge=knowledge is not None,
		tools=tools,
		instructions=instructions,
		memory_manager=memory_manager,
		enable_agentic_memory=mem_config.get("enable_agentic_memory", True),
		add_history_to_context=True,
		num_history_runs=mem_config.get("num_history_runs", 5),
		add_datetime_to_context=True,
		markdown=True,
		pre_hooks=[sanitize_session_history_pre_hook],
	)

	sub_agents = build_sub_agents(db, knowledge, workspace_dir=workspace_dir)
	all_agents = [main_agent] + sub_agents
	teams = build_teams(all_agents, db, workspace_dir=workspace_dir)
	schedules = build_schedules(workspace_dir=workspace_dir)
	knowledge_doc_paths = get_knowledge_docs_paths(workspace_dir=workspace_dir)
	knowledge_urls = load_knowledge_urls(workspace_dir=workspace_dir)

	return {
		"config": config,
		"db_url": db_url,
		"db": db,
		"knowledge": knowledge,
		"main_agent": main_agent,
		"fallback_model": fallback_model,
		"sub_agents": sub_agents,
		"teams": teams,
		"mcp_config": mcp_config,
		"tools_config": tools_config,
		"schedules": schedules,
		"knowledge_doc_paths": knowledge_doc_paths,
		"knowledge_urls": knowledge_urls,
		"workspace_dir": workspace_dir,
	}
