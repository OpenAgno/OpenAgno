# OpenAgno

[![PyPI version](https://badge.fury.io/py/openagno.svg)](https://pypi.org/project/openagno/)

OpenAgno is a declarative agent platform built on top of Agno. It packages a CLI, a FastAPI and AgentOS runtime, reusable workspace templates, tenant-aware provisioning, MCP connectivity, channel integrations, scheduler tooling, and PgVector-backed knowledge retrieval in a single repository.

The runtime is self-contained: you can install it from PyPI, run it locally, or deploy it behind your own infrastructure. It also exposes a documented HTTP contract (`/admin/*`, `/tenants/*`, `/knowledge/*`, channel webhooks) that any external control plane, dashboard, or orchestrator can consume to build multi-tenant products on top.

## What ships in this repo

- `openagno` CLI for workspace lifecycle, runtime control, validation, templates, and deployment helpers
- FastAPI runtime with AgentOS integration
- declarative `workspace/` configuration based on YAML and Markdown files
- built-in workspace templates for common agent setups
- tenant-aware routes and tenant-scoped workspace execution
- knowledge ingestion, listing, deletion, and semantic search over PgVector
- MCP client and MCP server support
- channel and protocol surfaces including WhatsApp, Slack, Telegram, AG-UI, and A2A
- scheduler support for recurring agent jobs
- public docs, IDE config files, and local service and container helpers

## Repository layout

- `openagno/commands` CLI commands exposed by `openagno`
- `openagno/core` tenant, workspace, and runtime primitives
- `openagno/templates` packaged starter templates
- `routes` FastAPI route builders for admin, tenants, knowledge, channels, and integrations
- `tools` optional runtime tools such as workspace and scheduler management
- `workspace` the default declarative workspace loaded by the runtime
- `workspaces` provisioned tenant workspaces when the local workspace store backend is used; this directory is ignored and should be empty after test cleanup
- `deploy` deployment scripts, including systemd installation
- `bridges` auxiliary channel bridges such as the WhatsApp QR bridge
- `docs` Mintlify documentation, including English and Spanish trees
- `ide-configs` ready-made MCP client config files for supported editors
- `tests` automated test suite

## Installation

Install from PyPI:

```bash
pip install openagno
```

Install from source:

```bash
git clone https://github.com/OpenAgno/OpenAgno.git
cd OpenAgno
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Install with validation and protocol extras:

```bash
pip install -e '.[dev,protocols]'
```

Python `>=3.10` is required.

## Agno compatibility

OpenAgno tracks the Agno `2.5.x` line and the direct dependency pins in this repo are aligned to Agno `2.5.14`, which was the latest stable release verified against PyPI on April 4, 2026.

The runtime code paths in this repository continue to rely on documented Agno `AgentOS`, interface routers, and `linked_to` knowledge isolation behavior, which remain present in the current Agno documentation for:

- `agno.os.AgentOS`
- `agno.os.interfaces.whatsapp`, `slack`, `telegram`, and `a2a`
- knowledge isolation through `linked_to` metadata and filter injection

## Quickstart

List available templates:

```bash
openagno templates list
```

Initialize a workspace from a template:

```bash
openagno init --template personal_assistant
```

Validate the workspace:

```bash
openagno validate
```

Start the runtime:

```bash
openagno start --foreground
```

For production or long-running WhatsApp traffic, prefer a single managed process such as the installed `systemd` unit. Do not run `systemctl`, `python gateway.py`, and `service_manager.py start` against the same workspace at the same time or you will create port and shutdown drift.

Health check:

```bash
curl http://127.0.0.1:8000/admin/health
```

## CLI surface

Main commands:

- `openagno init`
- `openagno start`
- `openagno stop`
- `openagno restart`
- `openagno status`
- `openagno logs`
- `openagno validate`

Grouped commands:

- `openagno create`
- `openagno add`
- `openagno templates`
- `openagno deploy`

The CLI entrypoint is defined in `openagno/cli.py` and maps directly to the command modules under `openagno/commands/`.

## Templates

The packaged template registry currently includes:

- `personal_assistant`
- `customer_support`
- `developer_assistant`
- `research_agent`
- `sales_agent`

Templates live in `openagno/templates/` and are copied into a new workspace through `openagno init`.

## Workspace model

The runtime is driven by the declarative files under `workspace/`.

Core files:

- `workspace/config.yaml`
- `workspace/instructions.md`
- `workspace/self_knowledge.md`
- `workspace/tools.yaml`
- `workspace/mcp.yaml`
- `workspace/schedules.yaml`
- `workspace/agents/*.yaml`
- `workspace/knowledge/urls.yaml`

Optional extension surface:

- `workspace/integrations/*`

The default workspace config in this repository currently enables:

- main agent id `agnobot-main`
- model provider `openai` with `gpt-4.1-mini`
- local database mode by default
- hybrid knowledge search
- agentic memory
- scheduler polling
- AgentOS embedded MCP server

The loader merges the base workspace with enabled integrations, builds runtime tools from `tools.yaml`, builds MCP clients from `mcp.yaml`, constructs the knowledge layer, and then instantiates the main agent, sub-agents, teams, and schedules.

## Runtime routes

The runtime exposes an admin surface plus tenant and knowledge operations.

Tenant routes:

- `GET /tenants`
- `POST /tenants`
- `GET /tenants/{tenant_id}`
- `PATCH /tenants/{tenant_id}`
- `DELETE /tenants/{tenant_id}`
- `GET /tenants/{tenant_id}/workspace`
- `PUT /tenants/{tenant_id}/workspace`
- `GET /tenants/{tenant_id}/workspace/inventory`
- `POST /tenants/{tenant_id}/workspace/sub-agents`
- `POST /tenants/{tenant_id}/workspace/sub-agents/{agent_id}/disable`
- `DELETE /tenants/{tenant_id}/workspace/sub-agents/{agent_id}`
- `POST /tenants/{tenant_id}/workspace/teams`
- `POST /tenants/{tenant_id}/workspace/teams/{team_id}/disable`
- `DELETE /tenants/{tenant_id}/workspace/teams/{team_id}`
- `POST /tenants/{tenant_id}/reload`
- `POST /tenants/{tenant_id}/agents/{agent_id}/runs`

The workspace inventory and mutation routes are used by external control planes such as OpenAgnoCloud to display the tenant's loaded sub-agents and teams without reaching into runtime internals. These routes only expose the HTTP contract; billing, signup, and hosted customer dashboards stay outside this OSS repo.

Knowledge routes:

- `POST /knowledge/upload`
- `POST /knowledge/ingest-urls`
- `GET /knowledge/list`
- `DELETE /knowledge/{doc_name}`
- `POST /knowledge/search`

The runtime also exposes the AgentOS and admin surfaces configured by `gateway.py`, including `/admin/health`.

WhatsApp modes currently supported by the runtime:

- `cloud_api`
- `qr_link`
- `dual`

The runtime supports QR-based WhatsApp linking through the optional Baileys bridge and the `/whatsapp-qr/*` routes. OpenAgno Cloud now exposes this as a first-class customer flow: after onboarding activation with `qr_link` mode, the customer sees a QR scanner page that polls the bridge and redirects to the dashboard once WhatsApp is connected.

For WhatsApp Cloud API (the official Meta Graph API), the runtime supports two deployment models:

- **Single-tenant**: driven by environment variables (`WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_APP_SECRET`). The runtime mounts a single `/whatsapp/webhook` via Agno. This is what the CLI wizard configures.
- **Multi-tenant**: each tenant brings its own Meta credentials. An external system (for example, a SaaS control plane running alongside this runtime) stores them encrypted (AES-256-GCM) in the Supabase `whatsapp_cloud_channels` table and the runtime exposes a webhook per tenant at `GET/POST /whatsapp-cloud/{tenant_id}/webhook`. Requires `CHANNEL_SECRETS_KEY` (a base64-encoded 32-byte key) to be shared between whichever side writes the encrypted credentials and the runtime that decrypts them. Production tenants should always store the tenant's Meta App Secret so POST requests can be verified with `X-Hub-Signature-256`.

The multi-tenant Cloud API webhook processes text, image, and audio payloads. Image payloads are passed to multimodal models when the tenant model supports images; audio payloads are passed to audio-capable models or transcribed with OpenAI Whisper when an OpenAI key is available. If neither path is configured, the runtime replies with an explicit audio capability message instead of silently dropping the event.

Production incident validation on July 3, 2026 confirmed the multi-tenant webhook must be observable through the whole inbound-to-outbound path: after `agent.arun()` completes the runtime logs the extracted `RunOutput` text length, sends Graph API replies from a protected send task, logs `Meta acepto ...` on success, and advances `last_send_at`. If the agent returns no extractable text, the runtime sends a safe fallback message and writes `last_error=agent_error:empty_response`; it must never silently `continue` after a completed run.

Operational validation on April 27, 2026 confirmed a real Meta webhook subscription and real inbound/outbound WhatsApp Cloud API traffic for tenant `test-datatensei`, including text and audio messages, with `last_error=null` and `last_send_at` set in `whatsapp_cloud_channels`. Do not commit the generated tenant workspace or any provider credentials used for that validation.

An external control plane can also drive the `whatsapp.mode` field in the workspace config (`qr_link`, `cloud_api`, or `dual`) over the existing `/tenants/{id}/workspace` HTTP contract without adding new APIs on the runtime side.

Operational notes for the tenant contract:

- collection routes support both `/tenants` and `/tenants/`
- tenant storage now fails fast with `503 Tenant storage unavailable` instead of hanging the runtime when the backing database is unavailable
- tenant storage uses defensive Postgres connections for hosted databases such as Supabase session pooling

## Multi-tenant execution

OpenAgno includes tenant-aware isolation through the tenant store and workspace store layers. Tenant execution scopes identity, metadata, and knowledge filters so each run stays bound to its tenant context.

Key runtime behaviors:

- tenant creation provisions a workspace copy from a selected template
- tenant updates can persist workspace config changes
- tenant runs scope `user_id`, `session_id`, metadata, and knowledge filters
- tenant knowledge retrieval uses isolated filters for vector search
- BYOK model credentials (`model.api_key`, `model.aws_access_key_id`, `model.aws_secret_access_key` and the same fields under `fallback`) are encrypted at rest (AES-256-GCM, `enc:v1:` values) in the `openagno_tenants` table when `CHANNEL_SECRETS_KEY` is configured, and decrypted transparently when the tenant is loaded. See `openagno/core/model_secrets.py`

This is the public multi-tenant contract any external control plane can consume.

The runtime should stay focused on execution. Plan policy, customer onboarding, billing entitlements, and operator rollout logic should live in whatever layer sits on top of this runtime and be translated into runtime configuration through the tenant HTTP contract.

## Test-state cleanup

Runtime-created tenant state is not canonical source. After local or hosted E2E validation, remove generated tenant workspaces and QR sessions before committing:

```bash
rm -rf workspaces/<test-slug>
rm -rf bridges/whatsapp-qr/session/<test-slug>
```

The April 27, 2026 Cloud/OSS validation purged the temporary `codex-*`, `datatensei-test`, and QR-session state after confirming WhatsApp Cloud API token probing and encrypted credential storage. The checked-in `workspace/` directory was restored to the default template state and should not carry customer or operator personalization.

## Knowledge and vector search

OpenAgno uses PostgreSQL with PgVector for knowledge storage and retrieval. The default workspace config is prepared for hybrid search and can run against local Postgres or a hosted database such as Supabase.

Supported document flow:

- upload files through the knowledge API
- ingest remote URLs
- list indexed content
- delete indexed content by document name
- search semantically through the knowledge index

The default workspace is configured to auto-ingest both local docs and declared URLs when enabled.

## Tools and MCP

`workspace/tools.yaml` defines built-in and optional tools.

Built-in defaults in this repo:

- `duckduckgo`
- `crawl4ai`
- `reasoning`

Optional tools present in the default config:

- `workspace`
- `scheduler_mgmt`
- `email`
- `tavily`
- `github`
- `audio`
- `shell`
- `spotify`
- `yfinance`
- `wikipedia`
- `arxiv`
- `calculator`
- `file_tools`
- `python_tools`

`workspace/mcp.yaml` defines external MCP connections. The default file includes examples for:

- Agno Docs over `streamable-http`
- Tavily over `streamable-http`
- Supabase over `stdio`
- GitHub over `stdio`

The runtime can also expose its own MCP server when `agentos.enable_mcp_server: true` is enabled in `workspace/config.yaml`.

## IDE integration

Ready-made MCP client configs are available in:

- `ide-configs/cursor-mcp.json`
- `ide-configs/vscode-mcp.json`
- `ide-configs/windsurf-mcp.json`

Public references:

- MCP docs: `https://docs.openagno.com/mcp`
- AI index: `https://docs.openagno.com/llms.txt`
- main docs: `https://docs.openagno.com`

## Running locally

### Python process

```bash
source .venv/bin/activate
openagno start --foreground
```

### Docker Compose

```bash
docker compose up --build
```

This repository includes:

- a `pgvector/pgvector:pg17` database service
- a `gateway` service running `python gateway.py`
- an optional `whatsapp-bridge` profile

### systemd service

Install the systemd unit:

```bash
sudo bash deploy/install-service.sh
```

That script installs:

- `openagno.service`
- `openagno-whatsapp-bridge.service` when the QR bridge is present and Node.js is available

## Documentation

Run the docs site locally:

```bash
cd docs
npm install
npm run dev
```

Validate docs:

```bash
cd docs
npm run validate
npm run broken-links
```

Spanish pages are published under `docs/es/`.

## Release and validation

Latest validation (July 2, 2026): `pytest -q` passes with 153 tests and `ruff check` is clean, including the new at-rest encryption coverage in `tests/test_model_secrets.py`.

Local verification used for the current `v1.3.0` closeout:

```bash
source .venv/bin/activate
pytest -q
python -m build
ruff check
python -m pip install --force-reinstall --no-deps dist/openagno-1.3.0-py3-none-any.whl
python -c "import openagno; print(openagno.__version__)"
```

Expected results:

- test suite passes
- `ruff check` is clean
- wheel build succeeds
- installed package reports `1.3.0`

## Current release posture

The repository content reflects the `1.3.0` closeout work. Publishing to PyPI remains a separate operational step triggered manually by the maintainer when the `v1.3.0` wheel is ready to go live.

## License

Apache 2.0.
