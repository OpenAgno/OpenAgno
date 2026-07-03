"""WhatsApp Cloud API multi-tenant router para el gateway.

Cada tenant expone su propio webhook en `/whatsapp-cloud/<tenant_id>/webhook`
con credenciales cifradas (AES-256-GCM) almacenadas en la tabla
`public.whatsapp_cloud_channels` de Supabase.

Decisiones:

- Usamos el UUID de `tenants.id` en el path (no el slug) porque la columna
  `webhook_path` en la migracion SQL esta definida como generated column a
  partir de ese UUID y es lo que mostramos al operador para pegar en Meta.
- El slug del runtime (`tenants.runtime_slug`) lo resolvemos con JOIN para
  invocar el TenantLoader del OSS, que opera sobre slugs de workspace.
- La clave maestra AES-256-GCM vive en la env var `CHANNEL_SECRETS_KEY`
  (32 bytes en base64). Debe ser identica en este runtime y en cualquier
  sistema externo que escriba las filas cifradas en Supabase.
- Reutilizamos la `DATABASE_URL` que ya construye el gateway para el bundle
  por defecto (misma conexion a Supabase que usa TenantStore).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx
import psycopg
from agno.utils.log import logger
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, Response

from openagno.core.tenant import DEFAULT_TENANT, normalize_tenant_id


GRAPH_API_HOST = "https://graph.facebook.com"
CHANNEL_SECRETS_KEY_ENV = "CHANNEL_SECRETS_KEY"


@dataclass(frozen=True)
class WhatsAppCloudConfig:
	"""Credenciales descifradas + slug de runtime para un tenant."""

	tenant_id: str  # UUID del Cloud (tabla tenants)
	runtime_slug: str  # slug del workspace OSS
	phone_number_id: str
	waba_id: str | None
	graph_api_version: str
	access_token: str
	verify_token: str
	app_secret: str | None


class ConfigNotFoundError(LookupError):
	"""No hay credenciales Cloud API guardadas para ese tenant_id."""


def _load_key() -> bytes:
	raw = os.getenv(CHANNEL_SECRETS_KEY_ENV, "").strip()
	if not raw:
		raise RuntimeError(
			f"{CHANNEL_SECRETS_KEY_ENV} no esta definido en el entorno; "
			"se requiere la misma clave base64 que usa el sistema externo que escribe las filas cifradas."
		)
	decoded = base64.b64decode(raw)
	if len(decoded) != 32:
		raise RuntimeError(
			f"{CHANNEL_SECRETS_KEY_ENV} debe decodificar a 32 bytes (AES-256); "
			f"recibidos {len(decoded)}."
		)
	return decoded


def _decrypt(cipher_b64: str, nonce_b64: str) -> str:
	"""Descifra un secreto almacenado como cipher+nonce en base64.

	El `cipher` incluye el auth tag en los ultimos 16 bytes (convencion
	de Node `crypto.createCipheriv('aes-256-gcm')` y compatible con
	`cryptography.hazmat.primitives.ciphers.aead.AESGCM`).
	"""
	key = _load_key()
	nonce = base64.b64decode(nonce_b64)
	ct_with_tag = base64.b64decode(cipher_b64)
	aesgcm = AESGCM(key)
	plaintext = aesgcm.decrypt(nonce, ct_with_tag, associated_data=None)
	return plaintext.decode("utf-8")


def _dsn_from_env() -> str:
	"""Construye un DSN postgres compatible con psycopg usando las vars del gateway."""
	# Preferencia: DATABASE_URL explicita (si el operador la definio).
	url = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
	if url:
		return url
	host = os.getenv("DB_HOST")
	port = os.getenv("DB_PORT", "5432")
	user = os.getenv("DB_USER")
	password = os.getenv("DB_PASSWORD")
	name = os.getenv("DB_NAME", "postgres")
	sslmode = os.getenv("DB_SSLMODE", "require")
	if not (host and user and password):
		raise RuntimeError("Falta configuracion de DB (DB_HOST/DB_USER/DB_PASSWORD) para WhatsApp Cloud API")
	return f"postgresql://{user}:{password}@{host}:{port}/{name}?sslmode={sslmode}"


def load_cloud_config(tenant_id: str) -> WhatsAppCloudConfig:
	"""Carga y descifra la config Cloud API para un `tenants.id` UUID."""
	dsn = _dsn_from_env()
	query = """
		SELECT
			w.tenant_id::text,
			w.phone_number_id,
			w.waba_id,
			w.graph_api_version,
			w.access_token_cipher, w.access_token_nonce,
			w.verify_token_cipher, w.verify_token_nonce,
			w.app_secret_cipher, w.app_secret_nonce,
			t.runtime_slug
		FROM public.whatsapp_cloud_channels w
		JOIN public.tenants t ON t.id = w.tenant_id
		WHERE w.tenant_id = %s
		LIMIT 1
	"""
	with psycopg.connect(dsn, connect_timeout=5) as conn:
		with conn.cursor() as cur:
			cur.execute(query, (tenant_id,))
			row = cur.fetchone()
	if row is None:
		raise ConfigNotFoundError(f"whatsapp_cloud_channels: no hay config para tenant_id={tenant_id}")

	(
		tenant_id_s,
		phone_number_id,
		waba_id,
		graph_api_version,
		access_token_cipher,
		access_token_nonce,
		verify_token_cipher,
		verify_token_nonce,
		app_secret_cipher,
		app_secret_nonce,
		runtime_slug,
	) = row

	access_token = _decrypt(access_token_cipher, access_token_nonce)
	verify_token = _decrypt(verify_token_cipher, verify_token_nonce)
	app_secret: str | None = None
	if app_secret_cipher and app_secret_nonce:
		app_secret = _decrypt(app_secret_cipher, app_secret_nonce)

	return WhatsAppCloudConfig(
		tenant_id=tenant_id_s,
		runtime_slug=runtime_slug or DEFAULT_TENANT,
		phone_number_id=phone_number_id,
		waba_id=waba_id,
		graph_api_version=graph_api_version or "v21.0",
		access_token=access_token,
		verify_token=verify_token,
		app_secret=app_secret,
	)


def _touch_column(tenant_id: str, column: str, error: str | None = None) -> None:
	"""Actualiza timestamp de auditoria (verified_at, last_event_at, last_send_at).

	`column` se hardcodea desde call sites; nunca viene de input de usuario.
	"""
	assert column in ("verified_at", "last_event_at", "last_send_at"), column
	dsn = _dsn_from_env()
	if error:
		sql = f"UPDATE public.whatsapp_cloud_channels SET {column} = now(), last_error = %s WHERE tenant_id = %s"
		params: tuple[Any, ...] = (error[:500], tenant_id)
	else:
		sql = f"UPDATE public.whatsapp_cloud_channels SET {column} = now(), last_error = NULL WHERE tenant_id = %s"
		params = (tenant_id,)
	try:
		with psycopg.connect(dsn, connect_timeout=5) as conn:
			with conn.cursor() as cur:
				cur.execute(sql, params)
	except Exception as exc:  # noqa: BLE001 - no queremos que un update de telemetria tire el webhook
		logger.warning(f"whatsapp-cloud: no pude actualizar {column} para {tenant_id}: {exc}")


def verify_signature(app_secret: str, body: bytes, signature_header: str | None) -> bool:
	"""Valida el header X-Hub-Signature-256 que Meta envia al webhook."""
	if not signature_header:
		return False
	expected = "sha256=" + hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
	return hmac.compare_digest(expected.lower(), signature_header.lower().strip())


async def send_text(cfg: WhatsAppCloudConfig, to: str, text: str) -> dict[str, Any]:
	"""Envia un mensaje de texto via Graph API y devuelve el JSON de Meta."""
	url = f"{GRAPH_API_HOST}/{cfg.graph_api_version}/{cfg.phone_number_id}/messages"
	payload = {
		"messaging_product": "whatsapp",
		"to": to,
		"type": "text",
		"text": {"body": text},
	}
	async with httpx.AsyncClient(timeout=20) as client:
		resp = await client.post(
			url,
			json=payload,
			headers={
				"Authorization": f"Bearer {cfg.access_token}",
				"Content-Type": "application/json",
			},
		)
	if resp.status_code >= 400:
		raise RuntimeError(f"graph_api_{resp.status_code}: {resp.text[:300]}")
	return resp.json()


async def download_media(cfg: WhatsAppCloudConfig, media_id: str) -> tuple[bytes, str | None]:
	"""Descarga media entrante desde Graph API.

	Meta entrega solo un `media_id` en el webhook. Primero se consulta el
	descriptor del media y luego se descarga la URL firmada usando el mismo
	Access Token del phone number.
	"""
	metadata_url = f"{GRAPH_API_HOST}/{cfg.graph_api_version}/{media_id}"
	headers = {"Authorization": f"Bearer {cfg.access_token}"}
	async with httpx.AsyncClient(timeout=30) as client:
		meta_resp = await client.get(metadata_url, headers=headers)
		if meta_resp.status_code >= 400:
			raise RuntimeError(f"graph_media_meta_{meta_resp.status_code}: {meta_resp.text[:300]}")
		metadata = meta_resp.json()
		media_url = metadata.get("url")
		if not isinstance(media_url, str) or not media_url:
			raise RuntimeError("graph_media_missing_url")
		mime_type = metadata.get("mime_type") if isinstance(metadata.get("mime_type"), str) else None

		media_resp = await client.get(media_url, headers=headers)
		if media_resp.status_code >= 400:
			raise RuntimeError(f"graph_media_download_{media_resp.status_code}: {media_resp.text[:300]}")
		return media_resp.content, mime_type


async def transcribe_audio_with_openai(
	audio_bytes: bytes,
	mime_type: str,
	api_key: str,
	*,
	model: str = "gpt-4o-mini-transcribe",
) -> str | None:
	"""Transcribe audio cuando el modelo del tenant no soporta audio nativo."""
	from openai import OpenAI

	suffix_map = {
		"audio/ogg": ".ogg",
		"audio/opus": ".ogg",
		"audio/mpeg": ".mp3",
		"audio/mp4": ".m4a",
		"audio/x-m4a": ".m4a",
		"audio/wav": ".wav",
		"audio/webm": ".webm",
		"audio/amr": ".amr",
		"audio/aac": ".aac",
	}
	root = (mime_type or "").lower().split(";")[0].strip()
	suffix = suffix_map.get(root, ".ogg")

	def _run() -> str | None:
		tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
		tmp.write(audio_bytes)
		tmp.close()
		try:
			client = OpenAI(api_key=api_key)
			with open(tmp.name, "rb") as fh:
				res = client.audio.transcriptions.create(model=model, file=fh)
			text = getattr(res, "text", None)
			return text if isinstance(text, str) and text.strip() else None
		finally:
			try:
				os.unlink(tmp.name)
			except OSError:
				pass

	try:
		return await asyncio.to_thread(_run)
	except Exception as exc:  # noqa: BLE001
		logger.error(f"whatsapp-cloud: transcripcion OpenAI fallo: {type(exc).__name__}: {exc}")
		return None


def _extract_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
	"""Extrae mensajes conversacionales del payload estandar de Meta.

	Ignora statuses (delivered/read), reactions y tipos no soportados. Retorna
	dicts con keys estables: id, from, type, text, media_id, mime_type.
	"""
	entries = payload.get("entry") or []
	result: list[dict[str, Any]] = []
	for entry in entries:
		changes = entry.get("changes") or []
		for change in changes:
			value = change.get("value") or {}
			messages = value.get("messages") or []
			for msg in messages:
				msg_type = msg.get("type")
				if msg_type == "text":
					text = (msg.get("text") or {}).get("body") or ""
					if not text:
						continue
					result.append(
						{
							"id": msg.get("id", ""),
							"from": msg.get("from", ""),
							"type": "text",
							"text": text,
							"media_id": None,
							"mime_type": None,
						}
					)
					continue

				if msg_type not in ("image", "audio"):
					continue
				media = msg.get(msg_type) or {}
				media_id = media.get("id")
				if not isinstance(media_id, str) or not media_id:
					continue
				result.append(
					{
						"id": msg.get("id", ""),
						"from": msg.get("from", ""),
						"type": msg_type,
						"text": media.get("caption", "") if msg_type == "image" else "",
						"media_id": media_id,
						"mime_type": media.get("mime_type") if isinstance(media.get("mime_type"), str) else None,
					}
				)
	return result


def _extract_response_text(response: Any) -> str | None:
	if response is None:
		return None
	if hasattr(response, "content") and response.content is not None:
		return str(response.content)
	if hasattr(response, "messages") and response.messages:
		for msg in reversed(response.messages):
			role = getattr(msg, "role", "")
			content = getattr(msg, "content", None)
			if role == "assistant" and content:
				return str(content)
	return str(response)


def _is_raw_provider_error(text: str) -> bool:
	lowered = text.lower()
	return (
		"error code:" in lowered
		or "non-retryable model provider error" in lowered
		or "canonical string for this request" in lowered
		or "string-to-sign" in lowered
		or "the request signature we calculated does not match" in lowered
	)


def _safe_provider_error_reply() -> str:
	return (
		"No pude responder porque el proveedor de IA rechazo la solicitud. "
		"Revisa la configuracion del modelo en OpenAgno Cloud e intenta de nuevo."
	)


# Deduplicacion de mensajes entrantes. Meta reintenta la entrega del webhook si
# no recibe HTTP 200 en pocos segundos; como el agente puede tardar decenas de
# segundos, esos reintentos entregan el MISMO mensaje (mismo `id`) y, sin este
# guard, cada reintento dispararia otra ejecucion del agente y otra respuesta
# duplicada al usuario. Guardamos los ids ya vistos en memoria con TTL. Un solo
# proceso uvicorn atiende el webhook, asi que un dict en memoria es suficiente.
_PROCESSED_MESSAGE_TTL_SECONDS = 900
_processed_messages: dict[str, float] = {}
_processed_messages_lock = threading.Lock()

# Referencias fuertes a los tasks de procesamiento en curso para que el
# recolector de basura no los cancele a mitad de ejecucion.
_background_tasks: set[Any] = set()

# Limite duro por run del agente. Si el modelo/las tools se cuelgan, el usuario
# recibe un aviso en vez de silencio infinito.
_AGENT_RUN_TIMEOUT_SECONDS = int(os.getenv("WHATSAPP_AGENT_TIMEOUT_SECONDS", "300"))

# Un solo run del agente por tenant a la vez. Mensajes rapidos consecutivos
# creaban runs concurrentes sobre el MISMO objeto Agent y la MISMA sesion MCP
# (con refresh_connection reconectando en paralelo), lo que dejaba runs
# colgados sin error. Serializar por tenant hace el comportamiento predecible;
# los mensajes se atienden en orden de llegada.
_tenant_run_locks: dict[str, asyncio.Lock] = {}


def _get_tenant_run_lock(runtime_slug: str) -> asyncio.Lock:
	lock = _tenant_run_locks.get(runtime_slug)
	if lock is None:
		lock = asyncio.Lock()
		_tenant_run_locks[runtime_slug] = lock
	return lock


def _agent_timeout_reply() -> str:
	return (
		"Tu solicitud esta tardando mas de lo esperado y la corte para no dejarte "
		"esperando. Intenta de nuevo con una consulta mas breve o en unos minutos."
	)


def _claim_message(msg_id: str) -> bool:
	"""Marca un mensaje como en proceso. Devuelve True si es nuevo (hay que
	procesarlo), False si ya fue visto dentro del TTL (reintento a descartar).

	Mensajes sin id no se pueden deduplicar, asi que siempre se procesan.
	"""
	if not msg_id:
		return True
	now = time.time()
	with _processed_messages_lock:
		expired = [k for k, ts in _processed_messages.items() if now - ts > _PROCESSED_MESSAGE_TTL_SECONDS]
		for k in expired:
			del _processed_messages[k]
		seen_at = _processed_messages.get(msg_id)
		if seen_at is not None and now - seen_at <= _PROCESSED_MESSAGE_TTL_SECONDS:
			return False
		_processed_messages[msg_id] = now
		return True


def create_router(*, get_tenant_loader: Any) -> APIRouter:
	"""Construye el router `/whatsapp-cloud/*` inyectando el TenantLoader.

	`get_tenant_loader` es un callable `() -> TenantLoader` para no acoplarnos
	al estado de FastAPI en el momento de import.
	"""
	router = APIRouter()

	@router.get("/whatsapp-cloud/{tenant_id}/webhook")
	async def verify_webhook(
		tenant_id: str,
		mode: str = Query(..., alias="hub.mode"),
		verify_token: str = Query(..., alias="hub.verify_token"),
		challenge: str = Query(..., alias="hub.challenge"),
	):
		if mode != "subscribe":
			raise HTTPException(status_code=403, detail="invalid_hub_mode")
		try:
			cfg = load_cloud_config(tenant_id)
		except ConfigNotFoundError:
			raise HTTPException(status_code=404, detail="tenant_not_configured")
		if not hmac.compare_digest(cfg.verify_token, verify_token):
			raise HTTPException(status_code=403, detail="invalid_verify_token")
		_touch_column(tenant_id, "verified_at")
		# Meta espera el challenge como texto plano (numerico).
		return Response(content=challenge, media_type="text/plain")

	async def _process_messages(
		cfg: WhatsAppCloudConfig, tenant_id: str, messages: list[dict[str, Any]]
	) -> None:
		"""Procesa mensajes y responde por Graph API. Corre en background para no
		bloquear el 200 del webhook (evita reintentos de Meta por timeout)."""
		tenant_loader = get_tenant_loader()
		runtime_slug = normalize_tenant_id(cfg.runtime_slug)

		try:
			bundle = tenant_loader.get_or_load(runtime_slug)
		except LookupError as exc:
			logger.error(f"[{runtime_slug}] whatsapp-cloud: no se pudo cargar workspace: {exc}")
			_touch_column(tenant_id, "last_event_at", error=f"workspace_missing:{exc}")
			return

		from agno.media import Audio as AgnoAudio, Image as AgnoImage
		from openagno.core.model_capabilities import get_model_capabilities

		agent = bundle["main_agent"]
		tenant_model_cfg = bundle.get("config", {}).get("model", {}) or {}
		tenant_caps = get_model_capabilities(tenant_model_cfg.get("id"))
		last_send_error: str | None = None

		for message in messages:
			from_jid = message["from"]
			text = message["text"]
			msg_type = message.get("type", "text")
			mime_type = message.get("mime_type")
			media_bytes: bytes | None = None
			media_id = message.get("media_id")
			logger.info(
				f"[{runtime_slug}] whatsapp-cloud: mensaje de {from_jid}: {text[:80]} (tipo={msg_type})"
			)
			try:
				if isinstance(media_id, str) and media_id:
					media_bytes, downloaded_mime = await download_media(cfg, media_id)
					mime_type = mime_type or downloaded_mime

				arun_kwargs: dict[str, Any] = {
					"user_id": f"{runtime_slug}:{from_jid}",
					"session_id": f"{runtime_slug}:{from_jid}",
				}

				if media_bytes and msg_type == "audio":
					if tenant_caps["audio"]:
						arun_kwargs["audio"] = [
							AgnoAudio(content=media_bytes, mime_type=mime_type or "audio/ogg")
						]
						if not text:
							text = "[Audio recibido]"
					else:
						whisper_key = None
						provider = str(tenant_model_cfg.get("provider", "")).strip().lower()
						if provider == "openai":
							whisper_key = tenant_model_cfg.get("api_key") or None
						if not whisper_key:
							whisper_key = os.getenv("OPENAI_API_KEY") or None
						if whisper_key:
							transcription = await transcribe_audio_with_openai(
								media_bytes,
								mime_type or "audio/ogg",
								whisper_key,
							)
							text = (
								f"[Transcripcion de audio]: {transcription}"
								if transcription
								else "[Recibi tu mensaje de audio pero no pude transcribirlo. Intenta de nuevo o enviame el mensaje como texto.]"
							)
						else:
							text = (
								"[Recibi tu mensaje de audio. Para que lo pueda procesar, "
								"configura una API key de OpenAI en tu workspace.]"
							)

				elif media_bytes and msg_type == "image":
					if tenant_caps["image"]:
						arun_kwargs["images"] = [
							AgnoImage(content=media_bytes, mime_type=mime_type or "image/jpeg")
						]
						if not text:
							text = "Describe o analiza esta imagen"
					else:
						text = (
							"[Recibi tu imagen pero mi modelo actual no puede analizarla. "
							"Cambia a un modelo multimodal desde el dashboard.]"
						)

				logger.info(f"[{runtime_slug}] whatsapp-cloud: ejecutando agente para {from_jid}...")
				started = time.monotonic()
				async with _get_tenant_run_lock(runtime_slug):
					result = await asyncio.wait_for(
						agent.arun(text, **arun_kwargs),
						timeout=_AGENT_RUN_TIMEOUT_SECONDS,
					)
				logger.info(
					f"[{runtime_slug}] whatsapp-cloud: agente respondio en {time.monotonic() - started:.1f}s"
				)
				reply_text = (_extract_response_text(result) or "").strip()
			except asyncio.TimeoutError:
				logger.error(
					f"[{runtime_slug}] whatsapp-cloud: agente supero el timeout de "
					f"{_AGENT_RUN_TIMEOUT_SECONDS}s; se avisa al usuario"
				)
				last_send_error = "agent_error:timeout"
				try:
					await send_text(cfg, from_jid, _agent_timeout_reply())
					_touch_column(tenant_id, "last_send_at")
				except Exception as send_exc:  # noqa: BLE001
					logger.error(
						f"[{runtime_slug}] whatsapp-cloud: fallo el aviso de timeout a {from_jid}: {send_exc}"
					)
				continue
			except Exception as exc:  # noqa: BLE001
				logger.error(f"[{runtime_slug}] whatsapp-cloud: agente fallo: {exc}")
				last_send_error = f"agent_error:{exc}"
				# Nunca dejar al usuario en silencio: enviar una respuesta segura
				# (sin detalles internos) para que sepa que algo fallo.
				try:
					await send_text(cfg, from_jid, _safe_provider_error_reply())
					_touch_column(tenant_id, "last_send_at")
				except Exception as send_exc:  # noqa: BLE001
					logger.error(
						f"[{runtime_slug}] whatsapp-cloud: fallo el aviso de error a {from_jid}: {send_exc}"
					)
				continue
			if reply_text and _is_raw_provider_error(reply_text):
				logger.error(
					f"[{runtime_slug}] whatsapp-cloud: proveedor devolvio error crudo; se envia respuesta segura"
				)
				last_send_error = "agent_error:model_provider_error"
				reply_text = _safe_provider_error_reply()
			if not reply_text:
				continue
			try:
				await send_text(cfg, from_jid, reply_text)
				_touch_column(tenant_id, "last_send_at")
			except Exception as exc:  # noqa: BLE001
				logger.error(
					f"[{runtime_slug}] whatsapp-cloud: send_text fallo a {from_jid}: {exc}"
				)
				last_send_error = f"send_failed:{exc}"

		_touch_column(tenant_id, "last_event_at", error=last_send_error)

	@router.post("/whatsapp-cloud/{tenant_id}/webhook")
	async def receive_webhook(tenant_id: str, request: Request):
		try:
			cfg = load_cloud_config(tenant_id)
		except ConfigNotFoundError:
			raise HTTPException(status_code=404, detail="tenant_not_configured")

		body = await request.body()
		if cfg.app_secret:
			signature = request.headers.get("x-hub-signature-256")
			if not verify_signature(cfg.app_secret, body, signature):
				_touch_column(tenant_id, "last_event_at", error="invalid_signature")
				raise HTTPException(status_code=401, detail="invalid_signature")

		try:
			payload = json.loads(body or b"{}")
		except json.JSONDecodeError:
			raise HTTPException(status_code=400, detail="invalid_json")

		if payload.get("object") != "whatsapp_business_account":
			# Meta envia el webhook generico compartido; ignoramos otros objetos.
			_touch_column(tenant_id, "last_event_at")
			return {"status": "ignored_non_wa"}

		messages = _extract_messages(payload)
		if not messages:
			_touch_column(tenant_id, "last_event_at")
			return {"status": "no_messages"}

		# Descartar reintentos de Meta: solo procesamos ids no vistos. Esto evita
		# respuestas duplicadas cuando el agente tarda y Meta reenvia el webhook.
		fresh = [m for m in messages if _claim_message(m.get("id", ""))]
		duplicates = len(messages) - len(fresh)
		if duplicates:
			logger.info(
				f"[{normalize_tenant_id(cfg.runtime_slug)}] whatsapp-cloud: "
				f"{duplicates} mensaje(s) duplicado(s) ignorado(s) (reintento de Meta)"
			)

		if fresh:
			# Responder 200 de inmediato y procesar en un task raiz del event loop,
			# desacoplado del ciclo de la request. No usamos BackgroundTasks de
			# Starlette porque corre dentro de los task groups/cancel scopes de los
			# middlewares y los MCP streamable-http fallan con "Cancelled via
			# cancel scope" al iniciarse ahi.
			task = asyncio.create_task(_process_messages(cfg, tenant_id, fresh))
			_background_tasks.add(task)
			task.add_done_callback(_background_tasks.discard)

		return {
			"status": "accepted",
			"received": len(messages),
			"queued": len(fresh),
			"duplicates": duplicates,
		}

	return router


def mount_on(app: FastAPI) -> None:
	"""Monta el router en el FastAPI app del gateway usando `app.state.tenant_loader`."""
	router = create_router(get_tenant_loader=lambda: app.state.tenant_loader)
	app.include_router(router)
	logger.info("WhatsApp Cloud API multi-tenant montado en /whatsapp-cloud/{tenant_id}/webhook")
