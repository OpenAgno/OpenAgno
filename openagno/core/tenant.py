"""Tenant models and Agno-native helpers for multi-tenant isolation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from agno.knowledge.knowledge import Knowledge
from sqlalchemy import JSON, Boolean, Column, DateTime, Integer, MetaData, String, Table, create_engine, select
from sqlalchemy.pool import NullPool

from openagno.core.model_secrets import (
	decrypt_workspace_config_secrets,
	encrypt_workspace_config_secrets,
)


DEFAULT_TENANT = "default"

_metadata = MetaData()
tenant_table = Table(
	"openagno_tenants",
	_metadata,
	Column("id", String(64), primary_key=True),
	Column("name", String(255), nullable=False),
	Column("slug", String(255), nullable=False, unique=True),
	Column("plan", String(64), nullable=False, default="free"),
	Column("workspace_config", JSON, nullable=False, default=dict),
	Column("created_at", DateTime(timezone=True), nullable=False),
	Column("active", Boolean, nullable=False, default=True),
	Column("max_agents", Integer, nullable=False, default=1),
	Column("max_messages_per_day", Integer, nullable=False, default=100),
)


@dataclass
class Tenant:
	id: str
	name: str
	slug: str
	plan: str = "free"
	workspace_config: dict[str, Any] | None = None
	created_at: datetime | None = None
	active: bool = True
	max_agents: int = 1
	max_messages_per_day: int = 100

	def to_dict(self, *, serialize: bool = True) -> dict[str, Any]:
		payload = asdict(self)
		payload["workspace_config"] = self.workspace_config or {}
		if serialize and self.created_at is not None:
			payload["created_at"] = self.created_at.isoformat()
		return payload


def slugify_tenant(value: str) -> str:
	value = (value or "").strip().lower()
	slug = "".join(ch if ch.isalnum() else "-" for ch in value)
	while "--" in slug:
		slug = slug.replace("--", "-")
	return slug.strip("-") or DEFAULT_TENANT


def normalize_tenant_id(value: str | None) -> str:
	return slugify_tenant(value or DEFAULT_TENANT)


def build_tenant_knowledge_filters(tenant_slug: str | None) -> dict[str, str] | None:
	if not tenant_slug:
		return None
	return {"linked_to": normalize_tenant_id(tenant_slug)}


def scope_identity(tenant_slug: str, identity: str | None, *, fallback: str) -> str:
	return f"{normalize_tenant_id(tenant_slug)}:{(identity or fallback).strip() or fallback}"


def get_tenant_scoped_knowledge(knowledge: Any, tenant_slug: str | None) -> Any:
	"""Return a tenant-scoped Knowledge view backed by the same Agno stores."""
	if knowledge is None or tenant_slug is None or not isinstance(knowledge, Knowledge):
		return knowledge

	return Knowledge(
		name=normalize_tenant_id(tenant_slug),
		description=knowledge.description,
		vector_db=knowledge.vector_db,
		contents_db=knowledge.contents_db,
		max_results=knowledge.max_results,
		readers=knowledge.readers,
		content_sources=knowledge.content_sources,
		isolate_vector_search=True,
	)


class TenantStore:
	"""Persist tenant definitions in the same SQL database used by OpenAgno."""

	def __init__(self, db_url: str):
		if db_url.startswith("sqlite"):
			self.engine = create_engine(db_url, future=True)
		else:
			self.engine = create_engine(
				db_url,
				future=True,
				connect_args={"connect_timeout": 5},
				pool_pre_ping=True,
				poolclass=NullPool,
			)
		_metadata.create_all(self.engine)

	def _row_to_tenant(self, row: Any) -> Tenant:
		return Tenant(
			id=row["id"],
			name=row["name"],
			slug=row["slug"],
			plan=row["plan"],
			workspace_config=decrypt_workspace_config_secrets(row["workspace_config"] or {}),
			created_at=row["created_at"],
			active=bool(row["active"]),
			max_agents=int(row["max_agents"]),
			max_messages_per_day=int(row["max_messages_per_day"]),
		)

	def list_tenants(self, *, active_only: bool = False) -> list[Tenant]:
		stmt = select(tenant_table).order_by(tenant_table.c.created_at.desc())
		if active_only:
			stmt = stmt.where(tenant_table.c.active.is_(True))
		with self.engine.begin() as conn:
			rows = conn.execute(stmt).mappings().all()
		return [self._row_to_tenant(row) for row in rows]

	def get_tenant(self, identifier: str) -> Tenant | None:
		value = (identifier or "").strip()
		if not value:
			return None
		stmt = select(tenant_table).where(
			(tenant_table.c.id == value) | (tenant_table.c.slug == slugify_tenant(value))
		)
		with self.engine.begin() as conn:
			row = conn.execute(stmt).mappings().first()
		return self._row_to_tenant(row) if row else None

	def create_tenant(
		self,
		*,
		name: str,
		slug: str | None = None,
		plan: str = "free",
		workspace_config: dict[str, Any] | None = None,
		max_agents: int = 1,
		max_messages_per_day: int = 100,
	) -> Tenant:
		tenant = Tenant(
			id=str(uuid4()),
			name=name.strip(),
			slug=slugify_tenant(slug or name),
			plan=plan,
			workspace_config=workspace_config or {},
			created_at=datetime.now(timezone.utc),
			active=True,
			max_agents=max_agents,
			max_messages_per_day=max_messages_per_day,
		)

		if self.get_tenant(tenant.slug) is not None:
			raise ValueError(f"Tenant slug already exists: {tenant.slug}")

		row = tenant.to_dict(serialize=False)
		row["workspace_config"] = encrypt_workspace_config_secrets(row["workspace_config"])
		with self.engine.begin() as conn:
			conn.execute(tenant_table.insert().values(**row))
		return tenant

	def update_tenant(self, identifier: str, **updates: Any) -> Tenant:
		current = self.get_tenant(identifier)
		if current is None:
			raise KeyError(identifier)

		payload: dict[str, Any] = {}
		for field in (
			"name",
			"plan",
			"workspace_config",
			"active",
			"max_agents",
			"max_messages_per_day",
		):
			if field in updates and updates[field] is not None:
				payload[field] = updates[field]

		if "workspace_config" in payload:
			payload["workspace_config"] = encrypt_workspace_config_secrets(
				payload["workspace_config"]
			)

		if "slug" in updates and updates["slug"]:
			new_slug = slugify_tenant(updates["slug"])
			existing = self.get_tenant(new_slug)
			if existing is not None and existing.id != current.id:
				raise ValueError(f"Tenant slug already exists: {new_slug}")
			payload["slug"] = new_slug

		if not payload:
			return current

		with self.engine.begin() as conn:
			conn.execute(
				tenant_table.update().where(tenant_table.c.id == current.id).values(**payload)
			)
		updated = self.get_tenant(current.id)
		if updated is None:
			raise KeyError(current.id)
		return updated

	def deactivate_tenant(self, identifier: str) -> Tenant:
		return self.update_tenant(identifier, active=False)
