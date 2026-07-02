# Auto-Conocimiento de OpenAgno

## Proveedores de Modelo Válidos

| Provider (config.yaml) | Import Agno | Modelos recomendados (marzo 2026) |
|------------------------|-------------|----------------------------------|
| `google` | `agno.models.google.Gemini` | `gemini-2.5-flash` (default), `gemini-2.5-pro`, `gemini-3-flash-preview` |
| `openai` | `agno.models.openai.OpenAIChat` | `gpt-4.1-mini`, `gpt-5-mini`, `gpt-4o-mini` |
| `anthropic` | `agno.models.anthropic.Claude` | `claude-sonnet-4-6`, `claude-haiku-3-5` |
| `aws_bedrock_claude` | `agno.models.aws.Claude` | `us.anthropic.claude-sonnet-4-6-v1:0` |
| `aws_bedrock` | `agno.models.aws.AwsBedrock` | `amazon.nova-pro-v1:0` |

**NUNCA usar**: `bedrock`, `aws`, `claude`, `gemini` como provider.
**NOTA**: `gemini-2.0-flash` sera retirado el 1 de junio de 2026. Usar `gemini-2.5-flash`.

## Tools Disponibles en el Loader

| Nombre (tools.yaml) | Clase | Requiere |
|---------------------|-------|----------|
| `duckduckgo` | `DuckDuckGoTools` | Nada (builtin) |
| `crawl4ai` | `Crawl4aiTools` | Nada (builtin) |
| `reasoning` | `ReasoningTools` | Nada (builtin) |
| `github` | `GithubTools` | `PyGithub` + `GITHUB_TOKEN` env var |
| `shell` | `ShellTools` | `base_dir` en config |
| `email` | `EmailTools` | `EMAIL_*` env vars |
| `tavily` | `TavilyTools` | `TAVILY_API_KEY` |
| `workspace` | `WorkspaceTools` | Nada |
| `scheduler_mgmt` | `SchedulerTools` | `GATEWAY_URL` |
| `audio` | `AudioTools` | `OPENAI_API_KEY` |
| `yfinance` | `YFinanceTools` | `yfinance` pip package |
| `wikipedia` | `WikipediaTools` | `wikipedia` pip package |
| `arxiv` | `ArxivTools` | `arxiv` pip package |
| `calculator` | `CalculatorTools` | Nada |
| `file_tools` | `FileTools` | Nada |
| `python_tools` | `PythonTools` | Nada (riesgo de seguridad) |

**NUNCA usar en sub-agentes**: `tavily_search`, `web_search`, `search` — no son nombres de tools válidos en el loader.
**OJO**: `github` es válido (F7). Requiere registrar `GithubTools` en `loader.py`.

## Cómo Crear un Sub-Agente Válido

```yaml
# workspace/agents/ejemplo.yaml
agent:
  name: "Nombre del Agente"
  id: "nombre-agente"  # sin espacios, lowercase
  model:
    provider: openai  # DEBE ser uno de la tabla de arriba
    id: gpt-4.1-mini
  instructions:
    - "Instrucciones específicas del agente."
  tools:
    - duckduckgo      # DEBE existir en la tabla de tools
    - crawl4ai
  config:
    tool_call_limit: 5
    enable_agentic_memory: false
    markdown: true
execution:
  type: local
```

## MCP Servers Disponibles

| Server | URL | Tipo |
|--------|-----|------|
| Agno Docs | `https://docs.agno.com/mcp` | streamable-http |
| Tavily | Requiere API key | streamable-http |
| Supabase | Requiere `npx` + access token | stdio |
| GitHub | Requiere `GITHUB_TOKEN` | stdio |

## Canales Disponibles

| Canal | Configuración |
|-------|---------------|
| `whatsapp` | Cloud API (Meta Business) o QR Link (Baileys bridge) o Dual |
| `slack` | Slack Bot con scopes OAuth |
| `telegram` | Bot via @BotFather |
| `ai_sdk` | Vercel AI SDK (experimental) |
| Web | Siempre disponible via os.agno.com |

## Reglas de Auto-Configuración

1. Antes de crear un sub-agente, **consultar este archivo** para validar provider y tools.
2. Si necesitas un tool que no existe, **consulta MCP de Agno** (`docs.agno.com/mcp`) para ver si hay un tool builtin o MCP disponible.
3. Los sub-agentes **NO heredan** los tools del agente principal. Debes declararlos explícitamente.
4. Los sub-agentes **NO heredan** MCP servers. Si necesitan acceso a MCP, deben configurarlo en su YAML.
5. Después de cualquier mutación del workspace, siempre llama `request_reload` para que se cargue.
6. Nunca uses nombres inventados de tools. Si no estás seguro, consulta `workspace/tools.yaml`.

## WorkspaceTools — métodos disponibles

El toolkit `workspace` te permite auto-configurarte desde el chat. Todos los cambios van contra **tu propio workspace** (no el de otros tenants). Cada mutación crea un backup automático y exige `request_reload` para surtir efecto.

### Lectura
- `read_workspace_file(filename)` — lee `config.yaml`, `instructions.md`, etc.
- `list_workspace()` — árbol de archivos del workspace.
- `list_sub_agents()` — enumera todos los sub-agentes con id, rol, modelo y tools.

### Instrucciones y tools del agente principal
- `update_instructions(new_instructions)` — reescribe `instructions.md`.
- `enable_tool(name)` / `disable_tool(name)` — activa o desactiva tools declarados en `tools.yaml`.
- `toggle_tool(name, enabled)` — equivalente con flag explícito.

### Modelo principal
- `set_model(provider, model_id, api_key?, aws_access_key_id?, aws_secret_access_key?, aws_region?)` — cambia el modelo en `config.yaml::model`. Si el usuario comparte credenciales por chat, el valor queda en texto plano en el YAML; recomiéndale rotarlas por el dashboard.

### Sub-agentes
- `create_sub_agent(name, agent_id, role, tools, instructions, model_provider?, model_id?)` — crea `agents/<id>.yaml`.
- `disable_sub_agent(agent_id)` — renombra a `.yaml.disabled` (reversible).
- `delete_sub_agent(agent_id)` — elimina con backup.

### Teams
- `create_team(team_id, name, mode, members, instructions?, model_provider?, model_id?)` — upsert en `agents/teams.yaml`. `mode` debe ser `coordinate|route|broadcast|tasks` y `members` deben ser IDs válidos (los propios sub-agentes o `agnobot-main`).

### MCP servers
- `add_mcp_server(name, transport, url?, command?, args?, headers?, env?, enabled?)` — upsert en `mcp.yaml`. Para `streamable-http`/`sse` usa `url`; para `stdio` usa `command`+`args`.
- `disable_mcp_server(name)` — baja `enabled=false` sobre una entrada existente.

### Archivo crudo
- `write_workspace_file(filename, content)` — edita cualquier archivo relativo al workspace (con backup). Útil cuando necesitas una mutación que no cubra ningún método específico.

### Reload
- `request_reload()` — tras cualquier mutación, invoca este método. En tenants no-default invalida solo tu cache en el TenantLoader; la siguiente request reconstruye tu workspace.

## Ejemplos de auto-configuración desde chat

- "Crea un sub-agente de ventas que use DuckDuckGo y Reasoning con claude-sonnet-4-6" → `create_sub_agent` → `request_reload`.
- "Activa el MCP de Supabase" → `add_mcp_server(name='supabase', transport='stdio', command='npx', args=['-y','@supabase/mcp-server-supabase'], env={...})` → `request_reload`.
- "Arma un team de research con agnobot-main y research-agent" → `create_team(team_id='research-team', mode='coordinate', members=['agnobot-main','research-agent'], ...)` → `request_reload`.
- "Cambia mi modelo principal a gpt-4o" → `set_model('openai','gpt-4o')` → `request_reload`.
- "Desactiva el tool shell" → `disable_tool('shell')` → `request_reload`.
