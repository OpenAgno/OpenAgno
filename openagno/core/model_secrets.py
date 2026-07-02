"""Cifrado en-sitio de credenciales BYOK de modelos dentro de workspace_config.

Los tenants pueden traer sus propias credenciales de proveedor de modelo
(`api_key`, `aws_access_key_id`, `aws_secret_access_key`) dentro de los
bloques `model` y `fallback` del workspace config. Este modulo cifra esos
valores antes de persistirlos en la tabla `openagno_tenants` y los descifra
al leerlos, de modo que la base de datos nunca contenga secretos en claro.

Formato del valor cifrado: ``enc:v1:<nonce_b64>:<cipher_b64>`` donde el
cipher incluye el auth tag GCM en los ultimos 16 bytes (misma convencion
que ``openagno/channels/whatsapp_cloud.py`` y que el control plane externo,
ambos usando la clave maestra ``CHANNEL_SECRETS_KEY``).

Si ``CHANNEL_SECRETS_KEY`` no esta configurada, las funciones degradan a
no-op para no romper despliegues OSS single-tenant que confian en el
``.env`` del servidor y no persisten credenciales por tenant.
"""

from __future__ import annotations

import base64
import os
from typing import Any

from agno.utils.log import logger

ENC_PREFIX = "enc:v1:"
CHANNEL_SECRETS_KEY_ENV = "CHANNEL_SECRETS_KEY"

_SECRET_FIELDS = ("api_key", "aws_access_key_id", "aws_secret_access_key")
_SECRET_BLOCKS = ("model", "fallback")


def _load_key() -> bytes | None:
	raw = os.getenv(CHANNEL_SECRETS_KEY_ENV, "").strip()
	if not raw:
		return None
	decoded = base64.b64decode(raw)
	if len(decoded) != 32:
		logger.warning(
			f"{CHANNEL_SECRETS_KEY_ENV} debe decodificar a 32 bytes; "
			f"recibidos {len(decoded)}. Se omite el cifrado de credenciales."
		)
		return None
	return decoded


def is_encrypted_value(value: Any) -> bool:
	return isinstance(value, str) and value.startswith(ENC_PREFIX)


def encrypt_value(plaintext: str) -> str:
	"""Cifra un valor individual. Devuelve el plaintext si no hay clave."""
	if is_encrypted_value(plaintext):
		return plaintext
	key = _load_key()
	if key is None:
		return plaintext
	from cryptography.hazmat.primitives.ciphers.aead import AESGCM

	nonce = os.urandom(12)
	cipher = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
	return (
		f"{ENC_PREFIX}"
		f"{base64.b64encode(nonce).decode('ascii')}:"
		f"{base64.b64encode(cipher).decode('ascii')}"
	)


def decrypt_value(value: str) -> str:
	"""Descifra un valor con formato enc:v1. Devuelve el valor tal cual si no aplica."""
	if not is_encrypted_value(value):
		return value
	key = _load_key()
	if key is None:
		logger.warning(
			"Se encontro una credencial cifrada pero CHANNEL_SECRETS_KEY no esta "
			"configurada; el modelo del tenant fallara hasta configurar la clave."
		)
		return value
	from cryptography.hazmat.primitives.ciphers.aead import AESGCM

	nonce_b64, cipher_b64 = value[len(ENC_PREFIX):].split(":", 1)
	nonce = base64.b64decode(nonce_b64)
	cipher = base64.b64decode(cipher_b64)
	plaintext = AESGCM(key).decrypt(nonce, cipher, None)
	return plaintext.decode("utf-8")


def _transform_config(config: Any, transform: Any) -> Any:
	if not isinstance(config, dict):
		return config
	result = dict(config)
	for block_name in _SECRET_BLOCKS:
		block = result.get(block_name)
		if not isinstance(block, dict):
			continue
		new_block = dict(block)
		for field in _SECRET_FIELDS:
			value = new_block.get(field)
			if isinstance(value, str) and value.strip():
				new_block[field] = transform(value)
		result[block_name] = new_block
	return result


def encrypt_workspace_config_secrets(config: dict[str, Any] | None) -> dict[str, Any] | None:
	"""Cifra credenciales de modelo antes de persistir el config en la DB."""
	if config is None:
		return None
	return _transform_config(config, encrypt_value)


def decrypt_workspace_config_secrets(config: dict[str, Any] | None) -> dict[str, Any] | None:
	"""Descifra credenciales de modelo al leer el config desde la DB."""
	if config is None:
		return None
	return _transform_config(config, decrypt_value)
