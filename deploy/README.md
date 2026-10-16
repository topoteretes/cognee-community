# cognee API + ArcadeDB (multi-tenant, Docker Compose)

Independent compose stacks, so the API can be rebuilt and redeployed without
touching the databases, and ingestion never competes with search for CPU:

| Stack | Role | Image |
|-------|------|-------|
| `deploy/docker-compose.yml` | cognee API, **query node** (search, recall, datasets, users) | `kolosek/cognee-arcadedb:1.6.1` (= `cognee/cognee:1.6.1` + this adapter, built from `Dockerfile`) |
| `deploy/docker-compose.ingest.yml` | cognee API, **ingest node** (add, cognify) | same image |
| `deploy/arcadedb/` | graph **and** vector store, one database per dataset | `arcadedata/arcadedb:26.10.1` |
| `deploy/postgres-ssl/` (or any PostgreSQL 14+) | relational / metadata store (users, datasets, permissions) | `kolosek/postgres-ssl:17.11` |
| `deploy/embeddings/` (optional) | local CPU embedding server shared by both nodes | `text-embeddings-inference` |
| `deploy/ui/` (optional) | cognee web UI (datasets, uploads, search, graph view) | `cognee/cognee-ui:1.6.1`, upstream's `cognee-frontend` |

Both cognee nodes run the same image with the same `.env`, share one S3 bucket
(uploaded files and system files; MinIO or AWS) and the databases, and expose the
full API. The cognee containers hold no state. The split is purely routing: a
cognify run keeps cognee's single async worker busy, so putting ingestion in its own
container keeps search latency flat. The database stacks publish their ports on the
Docker bridge gateway, which is what `host.docker.internal` resolves to inside the
containers, so they are reachable from containers on this host but not from the
network.

Multi-tenancy is cognee's backend access control (`ENABLE_BACKEND_ACCESS_CONTROL=true`):
every dataset is provisioned its own ArcadeDB database (named by the dataset UUID)
through the `arcadedb_graph_local` / `arcadedb_vector_local` handlers shipped by
this package, and every API call must be authenticated.

## Why a custom image?

The official `cognee/cognee` image does not know about community adapters, and its
`entrypoint.sh` runs migrations and then gunicorn against `cognee.api.client:app`
with no hook to import extra modules. The `Dockerfile` here installs this adapter
(from the checkout by default; set `ADAPTER_SOURCE` in `.env` to a PyPI requirement
to pin a published release) into cognee's venv and adds a `sitecustomize.py` so that
*every* Python process in the container imports
`cognee_community_hybrid_adapter_arcadedb.register` at start-up. The build fails if
the `arcadedb` provider does not end up registered.

The result is published as `kolosek/cognee-arcadedb:<cognee version>` and both
compose files pull it (`COGNEE_IMAGE` / `COGNEE_VERSION` in `.env`). To build it
yourself, layer the build override on either compose file; the build context is the
repository root (`..`), filtered by the root `.dockerignore`:

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
docker compose -f docker-compose.ingest.yml -f docker-compose.build.yml up -d --build
# publish:
docker compose -f docker-compose.yml -f docker-compose.build.yml build
docker compose -f docker-compose.yml -f docker-compose.build.yml push
```

## Run

Each stack is configured by its own `.env`; the compose files are only topology.
Containers write as uid 1000. The ArcadeDB stack has an `init` service that fixes
ownership of its bind mounts on every `up`, so it needs no sudo or manual mkdir.

```bash
cd deploy

# 1. PostgreSQL (or point DB_* at an existing server, see below)
(cd postgres-ssl && cp .env.example .env && docker compose up -d)

# 2. ArcadeDB (root password is a file, not an .env variable)
(cd arcadedb && cp .env.example .env \
  && (umask 077 && openssl rand -base64 24 > root_password) \
  && docker compose up -d)

# 3. cognee, query + ingest nodes
cp .env.example .env
# fill in: the S3 bucket + access key + endpoint (bucket must exist),
# DB_HOST/DB_PORT/DB_PASSWORD, GRAPH_DATABASE_PASSWORD (= arcadedb/root_password),
# the three FASTAPI_USERS_* secrets, CORS_ALLOWED_ORIGINS and LLM_API_KEY
# (openssl rand -hex 32 for secrets)
docker compose up -d                                          # query  -> :8000
docker compose -f docker-compose.ingest.yml up -d             # ingest -> :8001
docker compose logs -f cognee        # wait for "auth posture: ..." and gunicorn start
curl -f http://127.0.0.1:8000/health && curl -f http://127.0.0.1:8001/health

# 4. (optional) web UI; COGNEE_BACKEND_URL = the API as the browser reaches it
(cd ui && cp .env.example .env && docker compose up -d)       # UI -> :3000
```

Both nodes run cognee's migrations at start (serialized by a lock in cognee), so
start order between them does not matter. Before its own entrypoint, each node runs
`wait_for_backends.py`, which polls Postgres (a real login), ArcadeDB's ready endpoint
and the S3 endpoint until all answer, so the stacks can be started in any order and
a host reboot, where Docker starts every container at once, does not leave cognee
restart-looping on a database that is still coming up. It gives up after
`WAIT_FOR_BACKENDS_TIMEOUT` seconds (default 600) so a wrong password or host still
shows as a failing container; the reason is in `docker compose logs cognee`.
ArcadeDB on another host: set `GRAPH_DATABASE_URL` and `VECTOR_DB_URL` to `http://<host>:2480`
and publish the arcadedb stack with `ARCADEDB_BIND_ADDRESS=0.0.0.0`; the adapter
always uses ports 2480 and 7687, in plain text (see TLS under Operations).

ArcadeDB reads its root password from `arcadedb/root_password`. The root-running
`init` service copies it into a volume owned by uid 1000 (mode 400) that the
server reads at start, so it is not on the JVM command line or in `docker inspect`
and the host file can keep whatever owner created it (mode 600). A trailing
newline is ignored. Editing it and running `docker compose up -d` again re-stages
the file, recreates the container and re-creates root with the new password.

Where state lives (database stacks use bind mounts, `DATA_DIR`, default `./data`,
git-ignored; cognee uses the bucket):

| Where | Contents |
|------|----------|
| `arcadedb/data/databases` | graph + vector databases, one per dataset; by far the largest |
| `arcadedb/data/backups`, `arcadedb/data/log`, `arcadedb/data/certs` | ArcadeDB backups, server log, optional TLS keystore |
| `postgres-ssl/data` | PostgreSQL cluster |
| `s3://<bucket>/data` | uploaded source files, shared by both nodes |
| `s3://<bucket>/system/query`, `/system/ingest`, `/cognee/cache` | per-node cognee system files, cache |
| `embeddings/data` | model weights (optional stack) |

cognee stores every file's location in Postgres as an `s3://<bucket>/...` URI with no
hostname, so the endpoint (`AWS_ENDPOINT_URL`) can change, e.g. from the bridge
gateway to the proxy's HTTPS hostname, without touching data. Renaming the bucket
would need those rows rewritten.

## Routing

Both ports are loopback only. Put one TLS-terminating reverse proxy (Caddy, nginx,
Traefik) in front of them and route by path:

| Path | Node |
|------|------|
| `/api/v1/add`, `/api/v1/cognify`, `/api/v1/remember`, `/api/v1/memify`, `/api/v1/update` | ingest, `127.0.0.1:8001` |
| everything else under `/api/v1/` | query, `127.0.0.1:8000` |
| everything else (only with `ui/`) | UI, `127.0.0.1:3000` |

With the UI on the same hostname, set `COGNEE_BACKEND_URL` (in `ui/.env`) and
`CORS_ALLOWED_ORIGINS` (in `.env`) both to that `https://` origin. Route only
`/api/v1/` to cognee: the UI serves its own `/api/*` routes (runtime config, graph
and schema views) and must keep them. The UI container also calls
`COGNEE_BACKEND_URL` itself, so the public hostname must resolve from this host.
A UI on a different hostname works only within the same registrable domain, because
cognee's login cookie is `SameSite=Lax`. Swagger stays on the query port directly
(`http://127.0.0.1:8000/docs`).

Clients can also call the two ports directly (ingestion jobs → 8001, agents → 8000).
A request sent to the "wrong" node still works, it just competes for that node's CPU.
Tokens are valid on both nodes because the `FASTAPI_USERS_*` secrets are shared.
Give the proxy a large `client_max_body_size` (uploads) and a long read timeout
(cognify). ArcadeDB Studio is on `http://172.17.0.1:2480/` from this host (root / the
password in `arcadedb/root_password`; production mode hides it unless
`ARCADEDB_STUDIO_ENABLED=true`, the stack's default). If you proxy it, use its own
hostname (Studio expects to be served at `/`) and restrict access by IP or VPN, since
it is a root login.
If nginx itself runs in a container, loopback is not reachable from it: set
`COGNEE_BIND_ADDRESS=172.17.0.1` and use that address for all upstreams.

## PostgreSQL

cognee's relational store is not part of this stack. Any PostgreSQL 14+ reachable
from the cognee containers works (managed instance, another host, or `postgres-ssl/`
below); the role, password and database must already exist. In `deploy/.env`:

```bash
DB_HOST=db.example.com       # host.docker.internal for a server on this host
DB_PORT=5432
DB_USERNAME=cognee
DB_PASSWORD=...
DB_NAME=cognee_db
DATABASE_CONNECT_ARGS='{"ssl": "require"}'   # optional: refuse plaintext
```

A server on the same host must be published on an interface containers can reach:
all interfaces, or the Docker bridge gateway (`172.17.0.1`, which is what
`host.docker.internal` resolves to). `127.0.0.1` is not reachable from containers.

### postgres-ssl/

`postgres-ssl/docker-compose.yml` runs a standalone PostgreSQL 17 on the Railway
`postgres-ssl` image (TLS, pgvector, pgbackrest, dashboard-style in-place major
upgrades) with your own `postgresql.conf` / `pg_hba.conf` beside it:

```bash
(cd postgres-ssl && cp .env.example .env && docker compose up -d)
# deploy/.env: DB_HOST=host.docker.internal  DB_PORT=5405
#              (same DB_* credentials as postgres-ssl/.env)
```

## Local embeddings (optional)

Anthropic has no embedding models, so with Claude as the LLM embeddings come from
OpenAI or from the local `embeddings/` stack (CPU, no GPU; ~1 GB of model weights
downloaded once). Start it and enable the option-B `EMBEDDING_*` block in `.env`:

```bash
(cd embeddings && cp .env.example .env && docker compose up -d)
```

## First user

Either set `DEFAULT_USER_EMAIL` / `DEFAULT_USER_PASSWORD` in `.env` for the first boot
(cognee gives the built-in superuser that password once, then remove the lines), or
register through the API:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"alice@example.com","password":"change-me"}'

curl -X POST http://127.0.0.1:8000/api/v1/auth/login \
  -d 'username=alice@example.com&password=change-me'
```

Use the returned token as `Authorization: Bearer ...` on subsequent calls. Datasets
created by that user get their own ArcadeDB database; other users cannot see them
unless granted permissions via `/api/v1/permissions`. The same accounts sign in on
the web UI (`ui/`), which uses cognee's cookie login; the form's prefilled upstream
dev defaults are just placeholders to overwrite.

## Agent pods as tenants

Short-lived Claude Code containers (Optio tasks) use this deployment as memory
through cognee's `cognee-memory` Claude Code plugin, one cognee **user** per
workspace (= company = tenant), one dataset per workspace and a node set per
repo. The orchestrator provisions the user and key itself; `agents/README.md`
has the isolation model and `agents/provision_workspace.sh` does the same by
hand. Keep `POST /api/v1/auth/register` reachable only from the orchestrator,
since it is public by default.

## Operations

- **Backups**: Postgres (pg_dump, or pgbackrest on postgres-ssl), `arcadedb/data/databases`
  (ArcadeDB `backup database` command, or copy the folder while the server is
  stopped) and the bucket (`s3://<bucket>/data` holds the uploaded source files; a
  self-hosted MinIO is only as durable as its disk, so replicate or sync it).
- **Sizing ArcadeDB** (`arcadedb/.env`): CPU scales with concurrent ingestion, memory
  with the number of open tenant databases (heap is 75% of `ARCADEDB_MEMORY`). On the
  ingest node `INGEST_DATASET_QUEUE_MAX_CONCURRENT` bounds how many datasets are
  cognified at once; `DATABASE_MAX_LRU_CACHE_SIZE` how many each node keeps open.
- **Upgrades**: the adapter pins an exact cognee version, and the image tag follows
  it. Build and push the new `kolosek/cognee-arcadedb:<version>` (see "Why a custom
  image?"), bump `COGNEE_VERSION` in `.env`, then `docker compose pull && up -d` on
  both nodes; the first one up runs the relational and per-dataset migrations. The
  database stacks are untouched.
- **TLS to ArcadeDB**: off by default and not needed while the ports sit on the
  Docker bridge gateway. `arcadedb/.env.example` has a one-line `ARCADEDB_TLS_SETTINGS`
  block (PKCS12 keystore in `arcadedb/data/certs`) that turns on HTTPS on 2490 and
  TLS on Bolt 7687. The cognee adapter in this repo currently speaks plain `http://`
  and `bolt://` only, so for a cognee node on another machine put both hosts on a
  private network (WireGuard, Tailscale, cloud VPC) rather than exposing ArcadeDB
  with TLS alone.
- **Scaling further**: the cognee nodes are stateless. To move the ingest node to
  another machine, give it the same `.env` with database hosts and an S3 endpoint it
  can reach (the proxy's HTTPS hostname for MinIO), and publish the database stacks
  with `*_BIND_ADDRESS=0.0.0.0` behind a firewall or private network. The query node
  can likewise be replicated behind the proxy.
