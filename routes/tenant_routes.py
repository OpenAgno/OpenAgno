"""Tenant provisioning and tenant-aware agent execution routes."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import OperationalError, TimeoutError as SQLAlchemyTimeoutError
from starlette.concurrency import run_in_threadpool

from openagno.core.tenant import TenantStore, build_tenant_knowledge_filters, scope_identity
from openagno.core.tenant_loader import TenantLoader
from openagno.core.tenant_sync import sync_tenant_workspace_config
from openagno.core.workspace_store import WorkspaceStore
from security import verify_api_key
from tools.workspace_tools import WorkspaceTools


class TenantCreateRequest(BaseModel):
	name: str
	slug: str | None = None
	plan: str = "free"
	template: str = "personal_assistant"
	workspace_config: dict[str, Any] = Field(default_factory=dict)
	max_agents: int = 1
	max_messages_per_day: int = 100


class TenantUpdateRequest(BaseModel):
	name: str | None = None
	slug: str | None = None
	plan: str | None = None
	active: bool | None = None
	workspace_config: dict[str, Any] | None = None
	max_agents: int | None = None
	max_messages_per_day: int | None = None


class TenantRunRequest(BaseModel):
	message: str
	user_id: str | None = None
	session_id: str | None = None
	knowledge_filters: dict[str, Any] = Field(default_factory=dict)
	metadata: dict[str, Any] = Field(default_factory=dict)


class WorkspaceConfigRequest(BaseModel):
	config: dict[str, Any]


class SubAgentCreateRequest(BaseModel):
	name: str
	agent_id: str
	role: str = ""
	tools: list[str] = Field(default_factory=list)
	instructions: list[str] = Field(default_factory=list)
	model_provider: str | None = None
	model_id: str | None = None


class TeamCreateRequest(BaseModel):
	team_id: str
	name: str
	mode: str = "coordinate"
	members: list[str] = Field(default_factory=list)
	instructions: list[str] = Field(default_factory=list)
	model_provider: str | None = None
	model_id: str | None = None


def _extract_response_content(response: Any) -> str | None:
	if response is None:
		return None
	content = getattr(response, "content", None)
	if content is not None:
		return str(content)
	return None


def create_tenant_router(
	tenant_store: TenantStore,
	workspace_store: WorkspaceStore,
	agents_by_id: dict[str, Any],
	tenant_loader: TenantLoader | None = None,
) -> APIRouter:
	router = APIRouter(prefix="/tenants", tags=["tenants"], dependencies=[Depends(verify_api_key)])

	def _resolve_agent_for_tenant(tenant_slug: str, agent_id: str):
		"""Resuelve el agente correcto segun el tenant.

		- Para el tenant operador ("default"), usa el mapa global agents_by_id.
		- Para otros tenants, carga el workspace del tenant via tenant_loader
		  y busca el agente por id dentro de main_agent + sub_agents.
		"""
		if tenant_loader is None or tenant_slug == "default":
			return agents_by_id.get(agent_id)
		try:
			bundle = tenant_loader.get_or_load(tenant_slug)
		except LookupError:
			return None
		tenant_agents = [bundle["main_agent"]] + list(bundle.get("sub_agents", []))
		for agent in tenant_agents:
			if getattr(agent, "id", None) == agent_id:
				return agent
		return None

	async def _run_storage(callable_obj, *args, **kwargs):
		try:
			return await run_in_threadpool(callable_obj, *args, **kwargs)
		except (OperationalError, SQLAlchemyTimeoutError) as exc:
			raise HTTPException(status_code=503, detail="Tenant storage unavailable") from exc

	async def _list_tenants(active_only: bool = False) -> dict[str, Any]:
		tenants = await _run_storage(tenant_store.list_tenants, active_only=active_only)
		return {"tenants": [tenant.to_dict() for tenant in tenants], "count": len(tenants)}

	async def _resolve_tenant_or_404(tenant_id: str):
		tenant = await _run_storage(tenant_store.get_tenant, tenant_id)
		if tenant is None:
			raise HTTPException(status_code=404, detail="Tenant not found")
		return tenant

	def _workspace_tools_for_tenant(tenant_slug: str) -> WorkspaceTools:
		return WorkspaceTools(
			workspace_dir=workspace_store.workspace_path(tenant_slug),
			tenant_slug=tenant_slug,
			on_reload=tenant_loader.reload if tenant_loader is not None else None,
		)

	def _reject_s3_mutation() -> None:
		if workspace_store.backend == "s3":
			raise HTTPException(
				status_code=409,
				detail="Workspace inventory mutations require the local workspace backend",
			)

	def _raise_if_tool_error(result: str) -> None:
		if result.startswith("ERROR") or result.startswith("Error"):
			raise HTTPException(status_code=400, detail=result)

	def _runtime_inventory_overlay(tenant_slug: str) -> dict[str, Any]:
		loaded_sub_agents: list[str] = []
		loaded_teams: list[str] = []
		main_agent_id: str | None = None
		error: str | None = None
		if tenant_loader is None:
			return {
				"loaded_sub_agents": loaded_sub_agents,
				"loaded_teams": loaded_teams,
				"main_agent_id": main_agent_id,
				"error": "tenant_loader_unavailable",
			}
		try:
			bundle = tenant_loader.get_or_load(tenant_slug)
			main_agent_id = getattr(bundle.get("main_agent"), "id", None)
			loaded_sub_agents = [
				agent_id
				for agent_id in (getattr(agent, "id", None) for agent in bundle.get("sub_agents", []))
				if isinstance(agent_id, str)
			]
			loaded_teams = [
				team_id
				for team_id in (getattr(team, "id", None) for team in bundle.get("teams", []))
				if isinstance(team_id, str)
			]
		except Exception as exc:
			error = str(exc)
		return {
			"loaded_sub_agents": loaded_sub_agents,
			"loaded_teams": loaded_teams,
			"main_agent_id": main_agent_id,
			"error": error,
		}

	async def _inventory_for_tenant(tenant_id: str) -> dict[str, Any]:
		tenant, _synced = await _run_storage(
			sync_tenant_workspace_config,
			tenant_store,
			workspace_store,
			tenant_id,
		)
		if tenant is None:
			raise HTTPException(status_code=404, detail="Tenant not found")
		tools = _workspace_tools_for_tenant(tenant.slug)
		inventory = await run_in_threadpool(tools.workspace_inventory)
		runtime = _runtime_inventory_overlay(tenant.slug)
		loaded_sub_agents = set(runtime["loaded_sub_agents"])
		loaded_teams = set(runtime["loaded_teams"])
		for entry in inventory["sub_agents"]:
			entry["runtime_loaded"] = entry.get("id") in loaded_sub_agents
		for entry in inventory["teams"]:
			entry["runtime_loaded"] = entry.get("id") in loaded_teams
		return {
			"tenant": tenant.to_dict(),
			"backend": workspace_store.backend,
			"path": str(await run_in_threadpool(workspace_store.workspace_path, tenant.slug)),
			"inventory": {**inventory, "runtime": runtime},
		}

	async def _create_tenant(payload: TenantCreateRequest) -> dict[str, Any]:
		try:
			tenant = await _run_storage(
				tenant_store.create_tenant,
				name=payload.name,
				slug=payload.slug,
				plan=payload.plan,
				workspace_config=payload.workspace_config,
				max_agents=payload.max_agents,
				max_messages_per_day=payload.max_messages_per_day,
			)
		except ValueError as exc:
			raise HTTPException(status_code=409, detail=str(exc)) from exc

		workspace_path = await run_in_threadpool(
			workspace_store.provision,
			tenant.slug,
			template=payload.template,
			workspace_config=payload.workspace_config,
		)
		return {
			"tenant": tenant.to_dict(),
			"workspace_path": str(workspace_path),
			"workspace_backend": workspace_store.backend,
		}

	router.add_api_route("", _list_tenants, methods=["GET"], name="list_tenants_no_slash")
	router.add_api_route("/", _list_tenants, methods=["GET"], name="list_tenants")
	router.add_api_route("", _create_tenant, methods=["POST"], name="create_tenant_no_slash")
	router.add_api_route("/", _create_tenant, methods=["POST"], name="create_tenant")

	@router.get("/{tenant_id}")
	async def get_tenant(tenant_id: str) -> dict[str, Any]:
		tenant = await _run_storage(tenant_store.get_tenant, tenant_id)
		if tenant is None:
			raise HTTPException(status_code=404, detail="Tenant not found")
		return {"tenant": tenant.to_dict()}

	@router.patch("/{tenant_id}")
	async def update_tenant(tenant_id: str, payload: TenantUpdateRequest) -> dict[str, Any]:
		try:
			tenant = await _run_storage(
				tenant_store.update_tenant,
				tenant_id,
				**payload.model_dump(exclude_none=True),
			)
		except KeyError as exc:
			raise HTTPException(status_code=404, detail="Tenant not found") from exc
		except ValueError as exc:
			raise HTTPException(status_code=409, detail=str(exc)) from exc

		if payload.workspace_config:
			await run_in_threadpool(
				workspace_store.write_config,
				tenant.slug,
				payload.workspace_config,
			)
		return {"tenant": tenant.to_dict()}

	@router.delete("/{tenant_id}")
	async def deactivate_tenant(tenant_id: str) -> dict[str, Any]:
		try:
			tenant = await _run_storage(tenant_store.deactivate_tenant, tenant_id)
		except KeyError as exc:
			raise HTTPException(status_code=404, detail="Tenant not found") from exc
		return {"tenant": tenant.to_dict(), "status": "deactivated"}

	@router.get("/{tenant_id}/workspace")
	async def get_workspace_config(tenant_id: str) -> dict[str, Any]:
		tenant, _synced = await _run_storage(
			sync_tenant_workspace_config,
			tenant_store,
			workspace_store,
			tenant_id,
		)
		if tenant is None:
			raise HTTPException(status_code=404, detail="Tenant not found")
		return {
			"tenant": tenant.to_dict(),
			"backend": workspace_store.backend,
			"path": str(await run_in_threadpool(workspace_store.workspace_path, tenant.slug)),
			"config": await run_in_threadpool(workspace_store.read_config, tenant.slug),
		}

	@router.get("/{tenant_id}/workspace/inventory")
	async def get_workspace_inventory(tenant_id: str) -> dict[str, Any]:
		"""Devuelve inventario real del workspace del tenant, no del operador global."""
		return await _inventory_for_tenant(tenant_id)

	@router.post("/{tenant_id}/workspace/sub-agents")
	async def create_workspace_sub_agent(
		tenant_id: str,
		payload: SubAgentCreateRequest,
	) -> dict[str, Any]:
		_reject_s3_mutation()
		tenant = await _resolve_tenant_or_404(tenant_id)
		if not tenant.active:
			raise HTTPException(status_code=409, detail="Tenant is inactive")
		tools = _workspace_tools_for_tenant(tenant.slug)
		result = await run_in_threadpool(
			tools.create_sub_agent,
			payload.name,
			payload.agent_id,
			payload.role,
			payload.tools,
			payload.instructions,
			payload.model_provider,
			payload.model_id,
		)
		_raise_if_tool_error(result)
		return {"tenant": tenant.to_dict(), "status": "created", "message": result}

	@router.post("/{tenant_id}/workspace/sub-agents/{agent_id}/disable")
	async def disable_workspace_sub_agent(tenant_id: str, agent_id: str) -> dict[str, Any]:
		_reject_s3_mutation()
		tenant = await _resolve_tenant_or_404(tenant_id)
		if not tenant.active:
			raise HTTPException(status_code=409, detail="Tenant is inactive")
		tools = _workspace_tools_for_tenant(tenant.slug)
		result = await run_in_threadpool(tools.disable_sub_agent, agent_id)
		_raise_if_tool_error(result)
		return {"tenant": tenant.to_dict(), "status": "disabled", "message": result}

	@router.delete("/{tenant_id}/workspace/sub-agents/{agent_id}")
	async def delete_workspace_sub_agent(tenant_id: str, agent_id: str) -> dict[str, Any]:
		_reject_s3_mutation()
		tenant = await _resolve_tenant_or_404(tenant_id)
		if not tenant.active:
			raise HTTPException(status_code=409, detail="Tenant is inactive")
		tools = _workspace_tools_for_tenant(tenant.slug)
		result = await run_in_threadpool(tools.delete_sub_agent, agent_id)
		_raise_if_tool_error(result)
		return {"tenant": tenant.to_dict(), "status": "deleted", "message": result}

	@router.post("/{tenant_id}/workspace/teams")
	async def create_workspace_team(
		tenant_id: str,
		payload: TeamCreateRequest,
	) -> dict[str, Any]:
		_reject_s3_mutation()
		tenant = await _resolve_tenant_or_404(tenant_id)
		if not tenant.active:
			raise HTTPException(status_code=409, detail="Tenant is inactive")
		tools = _workspace_tools_for_tenant(tenant.slug)
		result = await run_in_threadpool(
			tools.create_team,
			payload.team_id,
			payload.name,
			payload.mode,
			payload.members,
			payload.instructions,
			payload.model_provider,
			payload.model_id,
		)
		_raise_if_tool_error(result)
		return {"tenant": tenant.to_dict(), "status": "created", "message": result}

	@router.post("/{tenant_id}/workspace/teams/{team_id}/disable")
	async def disable_workspace_team(tenant_id: str, team_id: str) -> dict[str, Any]:
		_reject_s3_mutation()
		tenant = await _resolve_tenant_or_404(tenant_id)
		if not tenant.active:
			raise HTTPException(status_code=409, detail="Tenant is inactive")
		tools = _workspace_tools_for_tenant(tenant.slug)
		result = await run_in_threadpool(tools.disable_team, team_id)
		_raise_if_tool_error(result)
		return {"tenant": tenant.to_dict(), "status": "disabled", "message": result}

	@router.delete("/{tenant_id}/workspace/teams/{team_id}")
	async def delete_workspace_team(tenant_id: str, team_id: str) -> dict[str, Any]:
		_reject_s3_mutation()
		tenant = await _resolve_tenant_or_404(tenant_id)
		if not tenant.active:
			raise HTTPException(status_code=409, detail="Tenant is inactive")
		tools = _workspace_tools_for_tenant(tenant.slug)
		result = await run_in_threadpool(tools.delete_team, team_id)
		_raise_if_tool_error(result)
		return {"tenant": tenant.to_dict(), "status": "deleted", "message": result}

	@router.put("/{tenant_id}/workspace")
	async def update_workspace_config(tenant_id: str, payload: WorkspaceConfigRequest) -> dict[str, Any]:
		tenant = await _run_storage(tenant_store.get_tenant, tenant_id)
		if tenant is None:
			raise HTTPException(status_code=404, detail="Tenant not found")
		config = await run_in_threadpool(workspace_store.write_config, tenant.slug, payload.config)
		tenant = await _run_storage(tenant_store.update_tenant, tenant.id, workspace_config=config)
		cache_evicted = False
		if tenant_loader is not None:
			cache_evicted = tenant_loader.reload(tenant.slug)
		return {"tenant": tenant.to_dict(), "config": config, "cache_evicted": cache_evicted}

	@router.post("/{tenant_id}/reload")
	async def reload_tenant_workspace(tenant_id: str) -> dict[str, Any]:
		"""Invalida el cache del tenant_loader para forzar recarga del workspace."""
		tenant, _synced = await _run_storage(
			sync_tenant_workspace_config,
			tenant_store,
			workspace_store,
			tenant_id,
		)
		if tenant is None:
			raise HTTPException(status_code=404, detail="Tenant not found")
		evicted = False
		if tenant_loader is not None:
			evicted = tenant_loader.reload(tenant.slug)
		return {"tenant": tenant.to_dict(), "status": "reloaded", "evicted": evicted}

	@router.post("/{tenant_id}/agents/{agent_id}/runs")
	async def run_agent_for_tenant(tenant_id: str, agent_id: str, payload: TenantRunRequest) -> dict[str, Any]:
		tenant = await _run_storage(tenant_store.get_tenant, tenant_id)
		if tenant is None:
			raise HTTPException(status_code=404, detail="Tenant not found")
		if not tenant.active:
			raise HTTPException(status_code=409, detail="Tenant is inactive")

		agent = _resolve_agent_for_tenant(tenant.slug, agent_id)
		if agent is None:
			raise HTTPException(status_code=404, detail="Agent not found")

		knowledge_filters = payload.knowledge_filters.copy()
		knowledge_filters.update(build_tenant_knowledge_filters(tenant.slug) or {})
		metadata = payload.metadata.copy()
		metadata.setdefault("tenant_id", tenant.id)
		metadata.setdefault("tenant_slug", tenant.slug)

		# El run corre en un task propio: los MCP streamable-http abren cancel
		# scopes de anyio que deben entrar y salir en el MISMO task; si viven en
		# el task de la request (anidado en los task groups de los middlewares de
		# Starlette) el scope se corrompe y el endpoint muere cancelado sin
		# respuesta ("No response returned").
		response = await asyncio.create_task(
			agent.arun(
				payload.message,
				user_id=scope_identity(tenant.slug, payload.user_id, fallback="user"),
				session_id=scope_identity(tenant.slug, payload.session_id, fallback="session"),
				knowledge_filters=knowledge_filters,
				metadata=metadata,
			)
		)
		return {
			"tenant": tenant.to_dict(),
			"agent_id": agent_id,
			"content": _extract_response_content(response),
			"knowledge_filters": knowledge_filters,
		}

	return router
