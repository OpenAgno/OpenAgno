# AGENTS.md

This file follows the [`AGENTS.md` convention](https://agents.md) so any agent-capable IDE (Claude Code, Cursor, Windsurf, VS Code with Copilot, Aider, etc.) can bootstrap its understanding of this repository from a single place. Read it end to end before opening other files.

## Scope

This repository is `OpenAgno`, an **open source agent runtime and CLI** built on top of Agno. It is fully self-contained: you can install it from PyPI, run it locally, and deploy it to your own infrastructure without depending on any hosted service.

OpenAgno owns:

- the packaged Python CLI (`openagno`)
- the FastAPI + AgentOS runtime (`gateway.py`, `loader.py`)
- the declarative `workspace/` source of truth
- packaged templates in `openagno/templates/`
- channel integrations under `openagno/channels/` plus the Node-based WhatsApp QR bridge in `bridges/whatsapp-qr/`
- knowledge ingestion and semantic search over PgVector
- MCP client/server support
- public Mintlify docs in `docs/` (English and `docs/es/`)
- CLI-facing skills for agent-capable IDEs in `.agents/skills/`

The runtime exposes a documented HTTP contract (`/admin/*`, `/tenants/*`, `/knowledge/*`, channel webhooks) that any external control plane, dashboard, or orchestrator can consume. OpenAgno itself does not ship a customer-facing dashboard, billing flows, or hosted deployment tooling; those concerns belong to whatever product or service is built on top of this runtime.

## Current baseline

- OSS version: `1.3.x`
- Agno floor: `agno[os,scheduler]>=2.5.14`
- Python: `>=3.10`
- WhatsApp QR bridge: Node 20 + `@whiskeysockets/baileys@7.0.0-rc.9` (ESM)
- Supabase tables consumed by OSS: `public.tenants`, `public.whatsapp_cloud_channels`
- License: Apache 2.0

## Prerequisites for a developer or agent to run this

Minimum to run the CLI and the runtime:

- Python 3.10+ with `venv`
- PostgreSQL with the `pgvector` extension (either local via `docker compose up -d db` using the included `pgvector/pgvector:pg17` service, or a hosted Postgres such as Supabase with `pgvector`)
- An LLM API key for the chosen provider (e.g. `GOOGLE_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or AWS Bedrock credentials)
- Optional: Node 20+ if you are going to enable the WhatsApp QR bridge

Platform-specific extras:

- WhatsApp Cloud API single-tenant: `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_VERIFY_TOKEN`, optional `WHATSAPP_APP_SECRET`
- WhatsApp Cloud API multi-tenant: `CHANNEL_SECRETS_KEY` (32 bytes base64, shared with whatever external system writes the encrypted credentials to the `whatsapp_cloud_channels` table)
- Slack: `SLACK_TOKEN`, `SLACK_SIGNING_SECRET`
- Telegram: `TELEGRAM_TOKEN`

## Setup commands

Canonical developer bootstrap:

```bash
git clone https://github.com/OpenAgno/OpenAgno.git
cd OpenAgno
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev,protocols]'
cp .env.example .env   # then edit values
openagno templates list
openagno init --template personal_assistant
openagno validate
openagno start --foreground
```

The CLI wizard (`openagno init`) interactively collects `.env` values. Non-interactive setups should fill `.env` directly.

## Repository map

- `gateway.py`: FastAPI entry point, mounts AgentOS interfaces and the custom OpenAgno routes, wires the `TenantLoader`, and attaches channel routers
- `loader.py`: resolves a workspace directory into an Agno agent bundle (`main_agent`, `sub_agents`, `teams`, `db`, `knowledge`, `config`). `load_workspace_from_dir(path)` is the canonical entry point; `load_workspace()` is a legacy wrapper
- `openagno/cli.py`: CLI bootstrap (maps to subcommands in `openagno/commands/`)
- `openagno/commands/`: packaged CLI commands (`init`, `start`, `stop`, `status`, `logs`, `validate`, `create`, `add`, `templates`, `deploy`)
- `openagno/core/tenant.py`: `Tenant` dataclass, `TenantStore`, `slugify_tenant`, `normalize_tenant_id`, knowledge-scoping helpers. The store encrypts BYOK model credentials before persisting `workspace_config` and decrypts them on read
- `openagno/core/model_secrets.py`: in-place AES-256-GCM encryption (`enc:v1:` values) for `model`/`fallback` credentials inside `workspace_config`, keyed by `CHANNEL_SECRETS_KEY`; degrades to no-op when the key is not configured
- `openagno/core/tenant_loader.py`: LRU cache of per-tenant workspace bundles. The gateway injects the default workspace as tenant `default` to avoid duplicate model objects
- `openagno/core/tenant_middleware.py`: resolves `X-Tenant-ID` (or query/path) for tenant-aware routes
- `openagno/core/workspace_store.py`: provisions tenant workspaces from templates and writes `config.yaml` + derived Markdown files
- `openagno/channels/`: OpenAgno-owned channel routers that complement the Agno-provided interfaces (currently `whatsapp_cloud.py` for multi-tenant Cloud API)
- `routes/tenant_routes.py`: `/tenants/*` CRUD plus workspace and `/runs`
- `routes/knowledge_routes.py`: `/knowledge/upload`, `/ingest-urls`, `/list`, `/{doc_name}`, `/search`
- `bridges/whatsapp-qr/index.js`: Baileys-based Node bridge, one session per tenant under `/sessions/:tenantSlug/*`. Routes to the OSS runtime via `POST /whatsapp-qr/incoming`
- `workspace/`: canonical checked-in workspace content (source of truth for the repo)
- `workspaces/`: runtime-created tenant workspace root (ignored, NOT canonical project content). Keep it empty after E2E cleanup.
- `openagno/templates/`: packaged starter templates used by `openagno init`
- `docs/`: public Mintlify documentation (English at root, Spanish under `docs/es/`)
- `ide-configs/`: copy-ready MCP configs for Cursor, VS Code, Windsurf, Claude Code
- `.agents/skills/`: IDE-agnostic skills that agent-capable IDEs can surface
- `management/`: runtime admin and validation helpers
- `tests/`: pytest suite (network layer is mocked, no external calls)
- `.github/`: issue and PR templates

## Setup + validation commands

Run the smallest relevant set first. Widen when the change is cross-cutting.

- Unit tests: `pytest -q`
- Focused tests: `pytest tests/test_<module>.py -x --tb=short`
- Lint: `ruff check .` (or `ruff check <paths>` when scoped)
- Package build: `python -m build`
- Docs validate: `cd docs && npm run validate`
- Docs links: `cd docs && npm run broken-links`

Minimum expectations by change type:

- Python runtime / CLI / routes: `pytest -q` and `ruff check`
- Packaging / dependency / version: add `python -m build`
- Docs / Mintlify nav: `npm run validate` and `npm run broken-links`
- WhatsApp QR bridge (`bridges/whatsapp-qr/`): `node --check index.js` and a manual boot in foreground

## Project-specific rules

- Public docs are English in `docs/`, Spanish mirrors in `docs/es/`. Keep them in sync when the surface changes.
- Do not reintroduce internal planning notes or `docs_plan/` material into the tracked tree.
- `workspace/` is the checked-in source of truth. `workspaces/` is runtime-generated, never committed, never hand-edited.
- After E2E validation, purge generated tenant state from `workspaces/*` and `bridges/whatsapp-qr/session/*` before committing. Do not leave customer/operator personalization in the checked-in `workspace/`.
- Do not hand-edit generated artifacts (`dist/`, `openagno.egg-info/`, `gateway.log`, `openagno.pid`) unless the user explicitly asks.
- Preserve the tenant HTTP surface in `routes/tenant_routes.py` and the channel routers in `openagno/channels/`; do not broaden the contract casually.
- Prefer backward-compatible changes to CLI, runtime routes, and workspace YAML shape.
- If a version or packaging change is made, keep `openagno/__init__.py`, `pyproject.toml`, and `docs/changelog.mdx` in sync.

## Public runtime contract

Any external control plane, dashboard, or orchestrator integrates with this runtime strictly over HTTP, using the documented surface:

- `GET /admin/health` (accepts `?tenant_slug=<slug>`; returns `tenant_model` and `tenancy.cache`)
- `POST /admin/reload`
- `POST /admin/fallback/activate`, `POST /admin/fallback/restore`
- `GET /tenants`, `POST /tenants`
- `GET /tenants/{tenant_id}`, `PATCH /tenants/{tenant_id}`, `DELETE /tenants/{tenant_id}`
- `GET /tenants/{tenant_id}/workspace`, `PUT /tenants/{tenant_id}/workspace`
- `GET /tenants/{tenant_id}/workspace/inventory`
- `POST /tenants/{tenant_id}/workspace/sub-agents`, `POST /tenants/{tenant_id}/workspace/sub-agents/{agent_id}/disable`, `DELETE /tenants/{tenant_id}/workspace/sub-agents/{agent_id}`
- `POST /tenants/{tenant_id}/workspace/teams`, `POST /tenants/{tenant_id}/workspace/teams/{team_id}/disable`, `DELETE /tenants/{tenant_id}/workspace/teams/{team_id}`
- `POST /tenants/{tenant_id}/reload`
- `POST /tenants/{tenant_id}/agents/{agent_id}/runs`
- `POST /whatsapp-qr/incoming` (Baileys bridge hands off inbound messages; body carries `tenant_slug`)
- `GET/POST /whatsapp-cloud/{tenant_id}/webhook` (per-tenant Meta webhook; requires `CHANNEL_SECRETS_KEY` to be set so the runtime can decrypt credentials from `public.whatsapp_cloud_channels`)

Multi-tenant runtime notes:

- `loader.load_workspace_from_dir(path)` lets the runtime load any workspace without relying on a global `WORKSPACE_DIR`. The legacy `load_workspace()` passes `WORKSPACE_DIR` automatically
- `TenantLoader` keeps an LRU cache (`OPENAGNO_TENANT_CACHE_SIZE`, default 32). The gateway pre-populates it with the default workspace as tenant `default`
- After every `PUT /tenants/{id}/workspace`, the caller must `POST /tenants/{id}/reload` to invalidate the cache
- `/tenants/{id}/workspace/inventory` is the read model external control planes should use to show runtime-loaded main agent, sub-agents, teams, model, and `WorkspaceTools` state
- Sub-agent/team mutation routes require `WorkspaceTools` to be enabled in the tenant workspace; if it is disabled, the control plane should show disabled controls instead of inventing inventory
- The Baileys bridge isolates one session per tenant under `/sessions/:tenantSlug/*`; inbound messages go to `POST /whatsapp-qr/incoming` with `tenant_slug` in the body
- Multi-tenant Cloud API resolves credentials from `public.whatsapp_cloud_channels` (AES-256-GCM cipher + nonce columns) using `CHANNEL_SECRETS_KEY`. See `openagno/channels/whatsapp_cloud.py`

BYOK (bring your own key) per tenant:

- `loader._build_single_model(provider, model_id, aws_region, api_key=?, aws_access_key_id=?, aws_secret_access_key=?)` accepts per-tenant credentials from `workspace/config.yaml` (`model.api_key`, `model.aws_access_key_id`, `model.aws_secret_access_key`)
- When a tenant provides its own API key (customer), Agno receives it as a kwarg
- When a tenant does not provide credentials (operator/default), Agno falls back to `os.environ` (the server-level `.env`)
- `GET /admin/health?tenant_slug=X` redacts credentials: returns only `provider`, `id`, `aws_region`
- At rest, BYOK credentials in `public.openagno_tenants.workspace_config` are AES-256-GCM encrypted as `enc:v1:<nonce_b64>:<cipher_b64>` values (see `openagno/core/model_secrets.py`); `TenantStore` decrypts on read so the loader keeps receiving plaintext kwargs. Requires `CHANNEL_SECRETS_KEY`

Do not widen this contract casually. In particular:

- do not add billing, subscription, or SaaS-specific logic to this repo
- do not bake in hard dependencies on any particular hosted service or vendor-specific UI
- keep the runtime usable standalone (someone should be able to `pip install openagno` and run it without any external control plane)

## Security and safety

- Never commit secrets, `.env`, tokens, or tenant data (see `.gitignore` and `SECURITY.md`)
- Preserve API-key protection on admin and tenant routes unless the user explicitly requests an auth change
- `WHATSAPP_SKIP_SIGNATURE_VALIDATION=true` exists for local development only; never set it in production
- Avoid destructive tenant or workspace cleanup unless the user clearly asked for it
- Multi-tenant Cloud API credentials (`access_token`, `verify_token`, `app_secret`) are AES-256-GCM encrypted in Supabase. The master key `CHANNEL_SECRETS_KEY` must be shared with whatever system populates the table; rotating it requires re-encrypting existing rows
- BYOK model credentials inside `openagno_tenants.workspace_config` are also encrypted with `CHANNEL_SECRETS_KEY` (`openagno/core/model_secrets.py`). Rotating the key requires re-encrypting those values too. Workspace files on the runtime host disk stay plaintext by design (the process needs them to call providers)

## Agent workflow

1. Read this file and `.agents/skills/openagno/SKILL.md` before searching broadly
2. Trust these instructions unless the code clearly contradicts them; in that case flag the contradiction in your response
3. Keep edits minimal and local to the task
4. Add or update tests when behavior changes
5. Update docs (English + Spanish) when a user-visible surface changes
6. Before finishing, run the relevant validation commands and report any gaps
7. If a task requires coordinated changes in an external system consuming this runtime, keep the OpenAgno change self-contained and clearly document any contract impact

## IDE and agent integration

- MCP config templates: `ide-configs/cursor-mcp.json`, `ide-configs/vscode-mcp.json`, `ide-configs/windsurf-mcp.json`, `ide-configs/claude-code-setup.sh`
- Skills for agent-capable IDEs: `.agents/skills/openagno/SKILL.md`, `.agents/skills/openagno-channels/SKILL.md`, `.agents/skills/mintlify/SKILL.md`
- Public docs MCP endpoint: `https://docs.openagno.com/mcp`
- `llms.txt`: `https://docs.openagno.com/llms.txt`
- `llms-full.txt`: `https://docs.openagno.com/llms-full.txt`

See [`docs/ide-integration.mdx`](./docs/ide-integration.mdx) for step-by-step setup per tool.

## Docs sync task

When the code surface changes, run the `DOCS_SYNC_PROMPT.md` playbook (at the repo root). Paste that prompt into your agent-capable IDE and let it verify the code base first, then update the Mintlify docs under `docs/` and `docs/es/` to match exactly what lives in `main`. The playbook covers version bumps, endpoint drift, env var drift, channel matrix updates, CLI command drift, and removal of legacy references.
