"""Tests unitarios del canal WhatsApp Cloud API multi-tenant.

Evitamos tocar la DB real: parcheamos `_dsn_from_env` y `psycopg.connect` con
un stub. Tambien cifamos con la misma clave AES-256-GCM que va a leer el
modulo para validar el round-trip con Node (AESGCM + authTag en los ultimos
16 bytes).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openagno.channels import whatsapp_cloud as wc


TEST_KEY_RAW = os.urandom(32)
TEST_KEY_B64 = base64.b64encode(TEST_KEY_RAW).decode("ascii")


def _encrypt(plaintext: str) -> tuple[str, str]:
	"""Cifra con el formato que usa el Cloud (Node): cipher = ct + authTag en base64."""
	nonce = os.urandom(12)
	aesgcm = AESGCM(TEST_KEY_RAW)
	ct_with_tag = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), associated_data=None)
	return (
		base64.b64encode(ct_with_tag).decode("ascii"),
		base64.b64encode(nonce).decode("ascii"),
	)


def _build_row(
	*,
	tenant_id: str = "11111111-1111-1111-1111-111111111111",
	runtime_slug: str = "acme",
	phone_number_id: str = "111122223333",
	waba_id: str | None = "999888777",
	graph_api_version: str = "v21.0",
	access_token: str = "EAA_test_access_token",
	verify_token: str = "verify_abc_hex",
	app_secret: str | None = "app_secret_test",
) -> tuple:
	at_c, at_n = _encrypt(access_token)
	vt_c, vt_n = _encrypt(verify_token)
	if app_secret:
		as_c, as_n = _encrypt(app_secret)
	else:
		as_c, as_n = (None, None)
	return (
		tenant_id,
		phone_number_id,
		waba_id,
		graph_api_version,
		at_c,
		at_n,
		vt_c,
		vt_n,
		as_c,
		as_n,
		runtime_slug,
	)


class _FakeCursor:
	def __init__(self, row: tuple | None):
		self._row = row

	def execute(self, *_args, **_kwargs):
		return None

	def fetchone(self):
		return self._row

	def __enter__(self):
		return self

	def __exit__(self, *_a):
		return False


class _FakeConn:
	def __init__(self, row: tuple | None):
		self._row = row

	def cursor(self):
		return _FakeCursor(self._row)

	def __enter__(self):
		return self

	def __exit__(self, *_a):
		return False


@contextmanager
def _patch_db(row: tuple | None):
	def _fake_connect(*_a, **_kw):
		return _FakeConn(row)

	with (
		patch.object(wc.psycopg, "connect", _fake_connect),
		patch.object(wc, "_dsn_from_env", lambda: "postgresql://fake"),
	):
		yield


@pytest.fixture(autouse=True)
def _env_key():
	old = os.environ.get("CHANNEL_SECRETS_KEY")
	os.environ["CHANNEL_SECRETS_KEY"] = TEST_KEY_B64
	yield
	if old is None:
		os.environ.pop("CHANNEL_SECRETS_KEY", None)
	else:
		os.environ["CHANNEL_SECRETS_KEY"] = old


def test_decrypt_roundtrip():
	ct, nonce = _encrypt("hola mundo")
	assert wc._decrypt(ct, nonce) == "hola mundo"


def test_decrypt_tampered_fails():
	ct, nonce = _encrypt("secreto")
	raw = bytearray(base64.b64decode(ct))
	raw[0] ^= 0xFF
	tampered = base64.b64encode(bytes(raw)).decode("ascii")
	with pytest.raises(Exception):
		wc._decrypt(tampered, nonce)


def test_load_cloud_config_maps_joined_slug():
	row = _build_row(runtime_slug="acme-2")
	with _patch_db(row):
		cfg = wc.load_cloud_config("11111111-1111-1111-1111-111111111111")
	assert cfg.runtime_slug == "acme-2"
	assert cfg.phone_number_id == "111122223333"
	assert cfg.access_token == "EAA_test_access_token"
	assert cfg.verify_token == "verify_abc_hex"
	assert cfg.app_secret == "app_secret_test"


def test_load_cloud_config_missing_app_secret():
	row = _build_row(app_secret=None)
	with _patch_db(row):
		cfg = wc.load_cloud_config("11111111-1111-1111-1111-111111111111")
	assert cfg.app_secret is None


def test_load_cloud_config_not_found():
	with _patch_db(None):
		with pytest.raises(wc.ConfigNotFoundError):
			wc.load_cloud_config("unknown")


def test_verify_signature():
	secret = "s3cr3t"
	body = b'{"hello":"world"}'
	sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
	assert wc.verify_signature(secret, body, sig) is True
	assert wc.verify_signature(secret, body, "sha256=wrong") is False
	assert wc.verify_signature(secret, body, None) is False


def test_extract_messages_filters_non_text():
	payload = {
		"object": "whatsapp_business_account",
		"entry": [
			{
				"changes": [
					{
						"value": {
							"messages": [
								{"id": "m1", "from": "5491111", "type": "text", "text": {"body": "hola"}},
								{"id": "m2", "from": "5491111", "type": "reaction"},
								{"id": "m3", "from": "5492222", "type": "text", "text": {"body": ""}},
								{"id": "m4", "from": "5493333", "type": "text", "text": {"body": "ok"}},
								{
									"id": "m5",
									"from": "5494444",
									"type": "image",
									"image": {"id": "media-img", "mime_type": "image/jpeg", "caption": "mira"},
								},
								{
									"id": "m6",
									"from": "5495555",
									"type": "audio",
									"audio": {"id": "media-audio", "mime_type": "audio/ogg"},
								},
							]
						}
					}
				]
			}
		],
	}
	msgs = wc._extract_messages(payload)
	assert [m["id"] for m in msgs] == ["m1", "m4", "m5", "m6"]
	assert msgs[2] == {
		"id": "m5",
		"from": "5494444",
		"type": "image",
		"text": "mira",
		"media_id": "media-img",
		"mime_type": "image/jpeg",
	}
	assert msgs[3]["media_id"] == "media-audio"


def _build_app(row: tuple | None, tenant_loader: Any | None = None):
	app = FastAPI()
	app.state.tenant_loader = tenant_loader
	router = wc.create_router(
		get_tenant_loader=lambda: app.state.tenant_loader,
		process_messages_inline=True,
	)
	app.include_router(router)
	return app


class _AgentResponse:
	def __init__(self, content: str):
		self.content = content


class _FakeAgent:
	def __init__(self, response: Any | None = None):
		self.calls: list[tuple[str, dict[str, Any]]] = []
		self.response = response if response is not None else _AgentResponse("respuesta del agente")

	async def arun(self, text: str, **kwargs):
		self.calls.append((text, kwargs))
		return self.response


class _FakeTenantLoader:
	def __init__(self, model: dict[str, Any] | None = None, response: Any | None = None):
		self.agent = _FakeAgent(response=response)
		self.model = model or {"provider": "openai", "id": "gpt-5-mini"}

	def get_or_load(self, _slug: str):
		return {
			"config": {"model": self.model},
			"main_agent": self.agent,
		}


def _wa_body(messages: list[dict[str, Any]]) -> bytes:
	return json.dumps(
		{
			"object": "whatsapp_business_account",
			"entry": [{"changes": [{"value": {"messages": messages}}]}],
		}
	).encode()


def test_webhook_verify_ok():
	row = _build_row(verify_token="vt_ok_xxxx")
	with (
		_patch_db(row),
		patch.object(wc, "_touch_column", lambda *_a, **_k: None),
	):
		app = _build_app(row)
		client = TestClient(app)
		resp = client.get(
			"/whatsapp-cloud/11111111-1111-1111-1111-111111111111/webhook",
			params={"hub.mode": "subscribe", "hub.verify_token": "vt_ok_xxxx", "hub.challenge": "42"},
		)
	assert resp.status_code == 200
	assert resp.text == "42"


def test_webhook_verify_wrong_token():
	row = _build_row(verify_token="correct")
	with (
		_patch_db(row),
		patch.object(wc, "_touch_column", lambda *_a, **_k: None),
	):
		app = _build_app(row)
		client = TestClient(app)
		resp = client.get(
			"/whatsapp-cloud/11111111-1111-1111-1111-111111111111/webhook",
			params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "42"},
		)
	assert resp.status_code == 403


def test_webhook_verify_missing_config():
	with (
		_patch_db(None),
		patch.object(wc, "_touch_column", lambda *_a, **_k: None),
	):
		app = _build_app(None)
		client = TestClient(app)
		resp = client.get(
			"/whatsapp-cloud/unknown-uuid/webhook",
			params={"hub.mode": "subscribe", "hub.verify_token": "x", "hub.challenge": "1"},
		)
	assert resp.status_code == 404


def test_raw_provider_errors_are_detected_for_safe_reply():
	raw_error = (
		"Error code: 403 - The request signature we calculated does not match. "
		"The Canonical String for this request should have been ..."
	)

	assert wc._is_raw_provider_error(raw_error) is True
	assert "Canonical String" not in wc._safe_provider_error_reply()


def test_extract_response_text_falls_back_to_assistant_message_when_content_empty():
	response = SimpleNamespace(
		content="",
		messages=[
			SimpleNamespace(role="user", content="hola"),
			SimpleNamespace(role="assistant", content="respuesta desde messages"),
		],
	)

	assert wc._extract_response_text(response) == "respuesta desde messages"


def test_extract_response_text_empty_structured_response_returns_none():
	response = SimpleNamespace(content="", messages=[], events=[])

	assert wc._extract_response_text(response) is None


def test_webhook_post_invalid_signature():
	row = _build_row(app_secret="top_secret")
	with (
		_patch_db(row),
		patch.object(wc, "_touch_column", lambda *_a, **_k: None),
	):
		app = _build_app(row)
		client = TestClient(app)
		resp = client.post(
			"/whatsapp-cloud/11111111-1111-1111-1111-111111111111/webhook",
			content=b'{"object":"whatsapp_business_account"}',
			headers={"content-type": "application/json", "x-hub-signature-256": "sha256=wrong"},
		)
	assert resp.status_code == 401


def test_webhook_post_no_app_secret_accepts_any_signature():
	row = _build_row(app_secret=None)
	body_dict = {"object": "whatsapp_business_account", "entry": []}
	body = json.dumps(body_dict).encode()
	with (
		_patch_db(row),
		patch.object(wc, "_touch_column", lambda *_a, **_k: None),
	):
		app = _build_app(row)
		client = TestClient(app)
		resp = client.post(
			"/whatsapp-cloud/11111111-1111-1111-1111-111111111111/webhook",
			content=body,
			headers={"content-type": "application/json"},
		)
	assert resp.status_code == 200
	assert resp.json()["status"] == "no_messages"


def test_webhook_dedupes_meta_retries_of_same_message_id():
	row = _build_row(app_secret=None, runtime_slug="tenant-api")
	loader = _FakeTenantLoader()
	sent: list[tuple[str, str]] = []

	async def _fake_send_text(_cfg, to: str, text: str):
		sent.append((to, text))
		return {"messages": [{"id": "wamid.sent"}]}

	body = _wa_body([
		{"id": "dup-1", "from": "5491111", "type": "text", "text": {"body": "hola"}},
	])
	with (
		_patch_db(row),
		patch.object(wc, "_touch_column", lambda *_a, **_k: None),
		patch.object(wc, "send_text", _fake_send_text),
	):
		# Aislar el estado global de dedup para este test.
		wc._processed_messages.clear()
		app = _build_app(row, tenant_loader=loader)
		with TestClient(app) as client:
			first = client.post(
				"/whatsapp-cloud/11111111-1111-1111-1111-111111111111/webhook",
				content=body,
				headers={"content-type": "application/json"},
			)
			second = client.post(
				"/whatsapp-cloud/11111111-1111-1111-1111-111111111111/webhook",
				content=body,
				headers={"content-type": "application/json"},
			)

	assert first.status_code == 200
	assert first.json()["queued"] == 1
	# El reintento de Meta con el mismo id no debe volver a ejecutar al agente.
	assert second.json()["queued"] == 0
	assert second.json()["duplicates"] == 1
	assert len(loader.agent.calls) == 1
	assert sent == [("5491111", "respuesta del agente")]


def test_webhook_post_text_runs_tenant_agent_and_sends_reply():
	row = _build_row(app_secret=None, runtime_slug="tenant-api")
	loader = _FakeTenantLoader()
	sent: list[tuple[str, str]] = []

	async def _fake_send_text(_cfg, to: str, text: str):
		sent.append((to, text))
		return {"messages": [{"id": "wamid.sent"}]}

	with (
		_patch_db(row),
		patch.object(wc, "_touch_column", lambda *_a, **_k: None),
		patch.object(wc, "send_text", _fake_send_text),
	):
		wc._processed_messages.clear()
		app = _build_app(row, tenant_loader=loader)
		with TestClient(app) as client:
			resp = client.post(
				"/whatsapp-cloud/11111111-1111-1111-1111-111111111111/webhook",
				content=_wa_body([
					{"id": "m-text", "from": "5491111", "type": "text", "text": {"body": "hola"}},
				]),
				headers={"content-type": "application/json"},
			)

	assert resp.status_code == 200
	assert resp.json()["queued"] == 1
	assert loader.agent.calls == [
		(
			"hola",
			{"user_id": "tenant-api:5491111", "session_id": "tenant-api:5491111"},
		)
	]
	assert sent == [("5491111", "respuesta del agente")]


def test_webhook_post_empty_agent_response_sends_safe_notice():
	row = _build_row(app_secret=None, runtime_slug="tenant-api")
	loader = _FakeTenantLoader(response=SimpleNamespace(content="", messages=[], events=[]))
	sent: list[tuple[str, str]] = []
	touches: list[tuple[str, str, str | None]] = []

	async def _fake_send_text(_cfg, to: str, text: str):
		sent.append((to, text))
		return {"messages": [{"id": "wamid.sent"}]}

	def _fake_touch(tenant_id: str, column: str, error: str | None = None):
		touches.append((tenant_id, column, error))

	with (
		_patch_db(row),
		patch.object(wc, "_touch_column", _fake_touch),
		patch.object(wc, "send_text", _fake_send_text),
	):
		wc._processed_messages.clear()
		app = _build_app(row, tenant_loader=loader)
		with TestClient(app) as client:
			resp = client.post(
				"/whatsapp-cloud/11111111-1111-1111-1111-111111111111/webhook",
				content=_wa_body([
					{"id": "m-empty", "from": "5491111", "type": "text", "text": {"body": "hola"}},
				]),
				headers={"content-type": "application/json"},
			)

	assert resp.status_code == 200
	assert sent == [("5491111", wc._empty_agent_reply())]
	assert ("11111111-1111-1111-1111-111111111111", "last_send_at", None) in touches
	assert (
		"11111111-1111-1111-1111-111111111111",
		"last_event_at",
		"agent_error:empty_response",
	) in touches


def test_webhook_post_image_downloads_media_for_multimodal_agent():
	row = _build_row(app_secret=None, runtime_slug="tenant-api")
	loader = _FakeTenantLoader(model={"provider": "openai", "id": "gpt-5-mini"})

	async def _fake_download(_cfg, media_id: str):
		assert media_id == "media-image"
		return b"jpeg-bytes", "image/jpeg"

	async def _fake_send_text(_cfg, _to: str, _text: str):
		return {"messages": [{"id": "wamid.sent"}]}

	with (
		_patch_db(row),
		patch.object(wc, "_touch_column", lambda *_a, **_k: None),
		patch.object(wc, "download_media", _fake_download),
		patch.object(wc, "send_text", _fake_send_text),
	):
		app = _build_app(row, tenant_loader=loader)
		with TestClient(app) as client:
			resp = client.post(
				"/whatsapp-cloud/11111111-1111-1111-1111-111111111111/webhook",
				content=_wa_body([
					{
						"id": "m-image",
						"from": "5491111",
						"type": "image",
						"image": {"id": "media-image", "mime_type": "image/jpeg"},
					},
				]),
				headers={"content-type": "application/json"},
			)

	assert resp.status_code == 200
	text, kwargs = loader.agent.calls[0]
	assert text == "Describe o analiza esta imagen"
	assert kwargs["images"][0].content == b"jpeg-bytes"
	assert kwargs["images"][0].mime_type == "image/jpeg"


def test_webhook_post_audio_transcribes_when_model_has_no_audio_native():
	row = _build_row(app_secret=None, runtime_slug="tenant-api")
	loader = _FakeTenantLoader(
		model={
			"provider": "openai",
			"id": "gpt-5-mini",
			"api_key": "tenant-openai-key",
		}
	)

	async def _fake_download(_cfg, media_id: str):
		assert media_id == "media-audio"
		return b"ogg-bytes", "audio/ogg"

	async def _fake_transcribe(audio_bytes: bytes, mime_type: str, api_key: str):
		assert audio_bytes == b"ogg-bytes"
		assert mime_type == "audio/ogg"
		assert api_key == "tenant-openai-key"
		return "audio transcrito"

	async def _fake_send_text(_cfg, _to: str, _text: str):
		return {"messages": [{"id": "wamid.sent"}]}

	with (
		_patch_db(row),
		patch.object(wc, "_touch_column", lambda *_a, **_k: None),
		patch.object(wc, "download_media", _fake_download),
		patch.object(wc, "transcribe_audio_with_openai", _fake_transcribe),
		patch.object(wc, "send_text", _fake_send_text),
	):
		app = _build_app(row, tenant_loader=loader)
		with TestClient(app) as client:
			resp = client.post(
				"/whatsapp-cloud/11111111-1111-1111-1111-111111111111/webhook",
				content=_wa_body([
					{
						"id": "m-audio",
						"from": "5491111",
						"type": "audio",
						"audio": {"id": "media-audio", "mime_type": "audio/ogg"},
					},
				]),
				headers={"content-type": "application/json"},
			)

	assert resp.status_code == 200
	assert loader.agent.calls[0][0] == "[Transcripcion de audio]: audio transcrito"
