# cognee-community-connector-argocd

An Argo CD data-source connector for [cognee](https://github.com/topoteretes/cognee):
read your Argo CD applications, their health and their sync history into memory.

It exposes a `dlt` source you hand to `cognee.remember(...)`. Each Argo CD application, and
each entry of its sync history, becomes a **normal document** in cognee (it flows through
chunking and LLM entity extraction), via cognee's document-mode marker. Project, repository,
destination cluster and resources are written into the text, so cognee can link an
application to the repository and cluster it deploys to.

Re-run the same `remember` call whenever you want to sync: only what changed is processed,
and applications deleted in Argo CD are forgotten.

## Requirements

- cognee **1.6.3** (pinned). The connector relies on table-scoped document cleanup
  (`DOCUMENT_SYNC_VERSION`), `PIPELINE_SCOPE_ATTR` and `guarded_rows`.
- Python 3.11 to 3.13.
- An Argo CD server you can reach over HTTPS, and an API token.

## Install

```bash
uv pip install cognee-community-connector-argocd
# or, from this monorepo:
cd packages/connector/argocd && uv sync
```

## Setup

### 1. Create a read-only API token

Use a dedicated local account that can only read applications. In `argocd-cm`:

```yaml
data:
  accounts.cognee: apiKey
```

In `argocd-rbac-cm`:

```yaml
data:
  policy.default: ""
  policy.csv: |
    p, role:app-reader, applications, get, */*, allow
    g, cognee, role:app-reader
```

Then create the token (it never expires unless you pass `--expires-in`):

```bash
argocd account generate-token --account cognee --expires-in 90d
```

The `*/*` object also matches applications in other namespaces when Argo CD runs with
"applications in any namespace".

**Argo CD omits what the token may not read.** The list endpoint returns HTTP 200 with fewer
applications, or none, when the token lacks permission for them. An empty result can
therefore mean a permission problem and not "nothing is deployed". The connector logs a
warning when the list comes back empty.

### 2. Reach the server

Argo CD is usually cluster-internal. Forward its port in a separate terminal:

```bash
kubectl port-forward svc/argocd-server -n argocd 8080:443
```

and use `https://localhost:8080` as the base URL.

- **Self-signed certificate.** The default install uses one. Pass `ca_bundle="/path/to/ca.pem"`
  to trust it. `verify_ssl=False` switches verification off; use it only for a throwaway local
  test, it logs a warning.
- **Plain HTTP.** `http://` is accepted for `localhost`, `127.0.0.1` and `::1` only, for a
  server started with `--insecure`.
- **Base path.** If Argo CD runs with `--rootpath /argo-cd`, include it:
  `https://gateway.example.com/argo-cd`.
- **Instance name.** Document ids contain an instance name. Every port-forward looks like
  `localhost`, so a localhost URL requires `instance_name`. For any other host it defaults to
  the host name.

### 3. Set your cognee keys

Set `LLM_API_KEY` like any other cognee run.

## Usage

```python
import cognee
from cognee_community_connector_argocd import argocd_source

await cognee.remember(
    # ARGOCD_BASE_URL, ARGOCD_AUTH_TOKEN and ARGOCD_INSTANCE_NAME can come from the environment
    argocd_source("https://localhost:8080", instance_name="demo", ca_bundle="argocd-ca.pem"),
    dataset_name="argocd",
    primary_key="id",
    write_disposition="merge",  # required: matches how the resource is declared
    max_rows_per_table=0,  # no row cap; the default stops after 50 rows
)

results = await cognee.search("Which applications are not healthy?", datasets=["argocd"])
```

`remember` defaults to `write_disposition="replace"` and would override the resource's own
`merge`, so always pass it as shown. See `examples/example.py` for the full flow.

| Argument | Env var | Default |
|---|---|---|
| `base_url` | `ARGOCD_BASE_URL` | required; may include a `--rootpath` prefix |
| `api_token` | `ARGOCD_AUTH_TOKEN` | required |
| `instance_name` | `ARGOCD_INSTANCE_NAME` | host name; **required** for localhost |
| `projects` | `ARGOCD_PROJECTS` (comma-separated) | every project the token can read |
| `app_names` | `ARGOCD_APPS` (comma-separated) | every application; an entry may be `namespace/name` |
| `app_namespace` | – | every namespace Argo CD has enabled |
| `selector` | – | no label selector |
| `include_resources` | – | `True`: resource roll-up and the resources that need attention |
| `include_history` | – | `True`: one document per sync-history entry |
| `history_limit` | – | all entries Argo CD keeps; set N for the newest N per application |
| `include_helm_values` | – | `False`: Helm parameters and values are left out |
| `verify_ssl` / `ca_bundle` | – | `True` / none |
| `timeout` | – | `60.0` seconds per request |
| `allow_mass_delete` | – | `False`: refuse to forget everything, or more than half of the known applications (at least 5), in one run |
| `resource_name` | – | `argocd_applications`; give each Argo CD its own name when several sync into one dataset |

After a run, `source.cognee_sync_stats` holds counts only (applications and history documents
emitted, applications forgotten, unchanged, skipped), never names or content.

## What is ingested

**Application document** (id `argocd:<instance>:<namespace>/<name>`): project, source
repository, path or chart and target revision, destination cluster and namespace, sync
policy, sync status, health status and its message, conditions, the last operation (phase,
message, revision, start and finish), a resource roll-up by kind with the resources that are
out of sync or not healthy (at most 20), container images, labels, and sentences that link
the application to its project, repository, cluster and managed resources.

**History document** (id `argocd:<instance>:<namespace>/<name>:history:<id>`): one per
deployment Argo CD remembers: revision, start and finish time, who or what started it, and
the source. A history entry never changes once written. Argo CD keeps
`spec.revisionHistoryLimit` entries per application.

**Not ingested:** rendered manifests, Kubernetes events, the resource tree, commit metadata,
annotations, `reconciledAt`/`observedAt`/`resourceVersion`/counters (they change without
meaning, and would make identical applications look changed).

**Secure by default**

- TLS is verified.
- Helm parameters and values are excluded unless `include_helm_values=True`. They can contain
  secrets.
- Credentials are removed from repository URLs (`https://user:password@host/...` becomes
  `https://host/...`).
- The token is only sent in the `Authorization` header. It is never logged, put in a URL or
  shown in an error message.

## Reused from cognee

- **`DOCUMENT_SOURCE_ATTR`**: marks the resource so each row becomes a text document that goes
  through normal cognify.
- **`PIPELINE_SCOPE_ATTR`, `DOCUMENT_SYNC_VERSION`, `guarded_rows`**: scope the dlt state per
  resource name, refuse cognee builds without table-scoped cleanup, and run the optional
  `check_active` authorization checkpoint around every row.
- **The google-calendar connector's structure**: one resource with `write_disposition="merge"`,
  `primary_key="id"`, and a `_deleted` hard-delete column, rows with
  `id`/`title`/`content`.

## How sync and forget-on-delete work

- **One request per run.** Each run lists the applications the token can read
  (`GET /api/v1/applications`) and works out the difference from the previous run, which is
  kept in the dlt resource state: per application its `resourceVersion`, the hash of its
  rendered text, and the ids of its history documents. Nothing else is stored: no token, no URL.
- **`resourceVersion` is only a shortcut.** Argo CD changes it on every reconciliation
  (`status.reconciledAt`, every few minutes by default), even when nothing meaningful changed.
  If it is equal to the stored value the application is not even rendered. If it differs, the
  application is rendered and **the hash of the text decides**: an unchanged text emits no row
  and only the stored `resourceVersion` is updated. A run where nothing changed emits no rows
  at all.
- **History documents are immutable.** Each sync-history entry is emitted once, when it first
  appears. Entries that later roll off Argo CD's `spec.revisionHistoryLimit`, or that you cut
  with a lower `history_limit`, stay in memory. They are forgotten with their application.
- **Deletions are found on every run** by comparing the full list with the stored one. An
  application that is gone from the list, or that Argo CD is deleting (it has a
  `deletionTimestamp`), is emitted as `{"id": ..., "_deleted": True}` together with all of its
  history documents. dlt removes those rows on `merge`, and cognee's orphan cleanup removes
  them from the graph and vector stores. Tombstones are yielded before the new rows, and the
  state is saved only after the last row, so a failed or interrupted run changes nothing and
  is simply repeated.
- **Guards against mass deletion.** Argo CD's list endpoint does not answer 403 for
  applications the token may not read: it silently leaves them out. A wrong token, a missing
  RBAC policy, a wrong `projects`/`selector` filter or an unhealthy server can therefore look
  like "everything was deleted". The sync raises, forgets nothing and keeps its state when
  - the list is empty while applications are known, or
  - more than half of the known applications, and at least 5, would be forgotten.

  Pass `allow_mass_delete=True` only when those applications were really deleted. An empty
  list on the very first run is not an error; it only logs a warning.
- **Removed access looks like deletion.** If you revoke the token's access to an application
  (or move it to a project the token cannot read), the next sync forgets it. So does changing
  the `projects`, `app_names`, `app_namespace` or `selector` filters: applications outside the
  new scope are forgotten, subject to the guards above.
- **Changing options re-renders.** The saved state includes a fingerprint of the filters, the
  `include_*` options, `history_limit` and an internal `RENDER_VERSION`. When it changes (you
  changed an option, or a connector upgrade changed how documents are written) every
  application is rendered again; only those whose text really changed are re-emitted. Turning
  `include_history` off forgets the stored history documents.
- **The merge arguments are required.** `cognee.remember` defaults to
  `write_disposition="replace"`, which overrides the resource's `merge`. Always pass
  `primary_key="id", write_disposition="merge", max_rows_per_table=0`.
- **Errors never pass silently.** HTTP 429/502/503/504 are retried up to five times with
  exponential backoff (and `Retry-After`); 401, 403, 404 and connection errors raise with a
  hint; nothing is swallowed and a failed run never forgets anything.
- **Cost.** One request per run, whatever has changed. The response holds every readable
  application in full, and Argo CD has no pagination or field selection, so it grows with the
  number of applications and their resources. Narrow it with the filters on large
  installations.

Not supported: Argo CD's watch stream (events can be dropped), a periodic full reconciliation,
a `userinfo` check before syncing, and commit metadata or messages.

## Privacy

**What is read:** from the applications the token can see: names, namespaces, project,
repository URL (without credentials), path, revision, destination, sync policy and status,
health and messages, conditions, operation results, resource names and health, container
images, labels and sync history. Helm values only with `include_helm_values=True`.
The connector only issues `GET` requests and never changes anything in Argo CD.

**Where it goes:** the text becomes cognee documents. During cognify it is sent to your
configured LLM and embedding providers, like any document you add to cognee. Use local models
if that matters to you. Health and operation messages come from your cluster and can contain
internal hostnames or error details.

**Logs and stats** contain counts only.

## Limitations

- Every run downloads the complete application list: Argo CD's list endpoint has no
  pagination and no field selection. Narrow it with `projects`, `app_names`, `app_namespace`
  or `selector` on large installations.
- Revoked access and filter changes are indistinguishable from deletion (see above).
- An application deleted and recreated under the same name between two syncs keeps its old
  history documents: history ids restart at 1 in the new application and are already known.- The shape of the API responses follows Argo CD's published swagger and has not yet been
  checked against a running server.

## Testing

```bash
uv run pytest tests/
```

No Argo CD and no API key are needed. `tests/fake_argocd.py` is an in-memory stand-in for
the Argo CD REST API with the same `get(url, params=..., verify=..., timeout=...)` shape as a
`requests` session, scripted status codes and a call log.

- `test_argocd_source.py` covers the factory checks (URL rules, localhost needs an instance
  name, environment fallbacks, no token in messages or logs), the HTTP client (retries,
  `Retry-After`, 401/403/404 hints, connection and TLS hints, `verify`, `ca_bundle`, repeated
  query parameters, base paths) and the cognee markers.
- `test_argocd_sync.py` covers the rendering of healthy and degraded applications, stable
  output for shuffled input, volatile fields, Helm values, credential stripping, the cap of
  20, missing fields, several sources and history documents. It also drives `_iter_rows` with
  a plain dict as state through the incremental cases: a second run with one list call and no
  rows, a `resourceVersion` change with equal text, a real change, a new history entry,
  removed and being-deleted applications and their history, tombstones before upserts, both
  guards and `allow_mass_delete`, failed calls that leave the state byte-identical, changed
  options and filters, and a state without secrets that survives JSON.
- `test_argocd_ingest.py` runs a real `dlt.pipeline` into SQLite: first sync, repeat runs that
  emit nothing, the cursor saved by a zero-row run, changed applications, new history,
  tombstoned rows that are physically gone, a failing API and the guards leaving the table
  unchanged.
- `test_argocd_forget.py` runs the real cognee pipeline (LLM and embeddings mocked) and checks
  that a deleted application and its history leave the graph, a run with no new rows forgets
  nothing, and an untouched application stays.

If pytest ends with `FileNotFoundError` while the repository sits on a Windows drive mounted
in WSL (`/mnt/<drive>/...`), point `TMPDIR` at a Linux directory such as `/dev/shm/argocd-tests`.
