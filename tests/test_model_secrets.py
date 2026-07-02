"""Tests de cifrado en-sitio de credenciales BYOK en workspace_config."""

import base64
import os
from pathlib import Path

import pytest
from sqlalchemy import select

from openagno.core.model_secrets import (
	decrypt_value,
	decrypt_workspace_config_secrets,
	encrypt_value,
	encrypt_workspace_config_secrets,
	is_encrypted_value,
)
from openagno.core.tenant import TenantStore, tenant_table


@pytest.fixture()
def secrets_key(monkeypatch):
	key = base64.b64encode(os.urandom(32)).decode("ascii")
	monkeypatch.setenv("CHANNEL_SECRETS_KEY", key)
	return key


def test_encrypt_value_round_trip(secrets_key):
	encrypted = encrypt_value("sk-proj-secreta")
	assert encrypted.startswith("enc:v1:")
	assert is_encrypted_value(encrypted)
	assert decrypt_value(encrypted) == "sk-proj-secreta"


def test_encrypt_value_no_recifra(secrets_key):
	once = encrypt_value("valor")
	assert encrypt_value(once) == once


def test_encrypt_is_noop_sin_clave(monkeypatch):
	monkeypatch.delenv("CHANNEL_SECRETS_KEY", raising=False)
	assert encrypt_value("plaintext") == "plaintext"


def test_workspace_config_round_trip(secrets_key):
	config = {
		"model": {
			"provider": "aws_bedrock_claude",
			"id": "us.anthropic.claude-opus-4-6-v1",
			"aws_access_key_id": "AKIAEXAMPLE",
			"aws_secret_access_key": "supersecreto",
			"aws_region": "us-east-1",
		},
		"fallback": {
			"enabled": False,
			"provider": "openai",
			"id": "gpt-5-mini",
			"api_key": "sk-fallback",
		},
		"channels": ["whatsapp"],
	}
	encrypted = encrypt_workspace_config_secrets(config)
	assert encrypted["model"]["aws_secret_access_key"].startswith("enc:v1:")
	assert encrypted["fallback"]["api_key"].startswith("enc:v1:")
	assert encrypted["model"]["provider"] == "aws_bedrock_claude"
	assert encrypted["model"]["aws_region"] == "us-east-1"
	assert decrypt_workspace_config_secrets(encrypted) == config


def test_tenant_store_cifra_en_db_y_descifra_al_leer(secrets_key, tmp_path: Path):
	db_url = f"sqlite:///{tmp_path / 'tenants.db'}"
	store = TenantStore(db_url)
	config = {
		"model": {"provider": "openai", "id": "gpt-5.2", "api_key": "sk-cliente"},
	}
	tenant = store.create_tenant(name="Acme", workspace_config=config)

	# En la fila persistida el secreto queda cifrado
	with store.engine.begin() as conn:
		row = conn.execute(
			select(tenant_table).where(tenant_table.c.id == tenant.id)
		).mappings().first()
	assert row["workspace_config"]["model"]["api_key"].startswith("enc:v1:")

	# Al leer via el store el secreto vuelve en claro
	loaded = store.get_tenant(tenant.slug)
	assert loaded is not None
	assert loaded.workspace_config["model"]["api_key"] == "sk-cliente"

	# update_tenant tambien cifra
	store.update_tenant(
		tenant.id,
		workspace_config={
			"model": {"provider": "openai", "id": "gpt-5.2", "api_key": "sk-rotada"},
		},
	)
	with store.engine.begin() as conn:
		row = conn.execute(
			select(tenant_table).where(tenant_table.c.id == tenant.id)
		).mappings().first()
	assert row["workspace_config"]["model"]["api_key"].startswith("enc:v1:")
	reloaded = store.get_tenant(tenant.id)
	assert reloaded.workspace_config["model"]["api_key"] == "sk-rotada"
