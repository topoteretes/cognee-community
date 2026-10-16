# Agent pods as cognee tenants

How short-lived Claude Code agent containers use this deployment as persistent
memory with one company = one workspace = one isolated tenant. The integration
itself lives in the orchestrator (Optio: `docs/cognee-memory.md` there); this
folder holds the server-side pieces.

| File | Purpose |
|------|---------|
| `provision_workspace.sh` | creates one tenant's cognee user + API key by hand and prints the pod env as JSON. Optio does the same three calls itself on workspace creation; use this for other orchestrators, for testing, or to repair a tenant |

## Model

- **Tenant = cognee user.** Every workspace gets `ws-<id>@optio.internal` and
  an API key. Datasets that user creates are its own ArcadeDB databases, every
  endpoint authorizes against the caller, and a fresh user holds no grants on
  anything else. Pods carry only that key; the admin / superuser account never
  leaves the server.
- **Not cognee "agents".** `/api/v1/agents` creates sub-users under a parent:
  they inherit the parent's tenant, the parent sees everything they write, and
  the Claude Code plugin's default *shared agent memory* puts all sub-users of
  one parent into one role with read/write on the parent's datasets. Hanging
  every workspace off one admin user would make that role a cross-tenant path.
  Pods therefore run as the workspace user directly, with
  `COGNEE_SHARED_AGENT_MEMORY=false` so the plugin never mints sub-users.
- **Dataset per workspace, node set per repo.** `COGNEE_PLUGIN_DATASET=ws-<id>`
  for every pod of the workspace and `COGNEE_PROJECT_NODE_SET=<repo-slug>` per
  task. One graph per company lets facts that span its repos link up, a
  single-repo company gets the same layout as a per-repo one, and the database
  count tracks companies rather than repositories, which is what ArcadeDB
  memory and cognee's per-node engine cache (`DATABASE_MAX_LRU_CACHE_SIZE`)
  scale with.
- **Client.** The `cognee-memory` Claude Code plugin (hooks: recall on every
  prompt, session sync on exit) rather than MCP tools, which a headless agent
  may never call. The orchestrator enables the plugin per run only for
  workspaces that have a tenant key; every other run leaves it disabled.

## Provision a tenant by hand

```bash
COGNEE_BASE_URL=https://mem.litetracker.com deploy/agents/provision_workspace.sh acme
```

Prints JSON with the user, password (needed only to mint replacement keys),
key and the `pod_env` block. Rerun with `COGNEE_WS_PASSWORD=<stored password>`
to mint a new key. Offboarding: delete the tenant's datasets
(`DELETE /api/v1/datasets/<id>` as that user) and then the user.

## On the cognee side

- **Registration is public** (`POST /api/v1/auth/register`). Any caller can
  create its own isolated account and spend LLM budget. In the reverse proxy,
  allow that path only from the orchestrator's address.
- **Routing**: the plugin writes through `/api/v1/remember` (ingest node) and
  reads through `/api/v1/recall` (query node); both are in the routing table in
  `../README.md`.
- **Sizing**: set `DATABASE_MAX_LRU_CACHE_SIZE` on each node above the number
  of workspaces with a running task at the same time, and give ArcadeDB heap
  to match (`arcadedb/.env`).
