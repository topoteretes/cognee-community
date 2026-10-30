# cognee-community-connector-jenkins

A Jenkins data-source connector for [cognee](https://github.com/topoteretes/cognee): sync your
CI history into memory and ask questions like *"why did the nightly build fail last week?"* or
*"which jobs started failing after the dependency bump?"*.

It exposes a `dlt` resource you hand to `cognee.remember(...)`, reusing cognee's existing DLT
ingestion path (`resolve_dlt_sources` → `ingest_dlt_source` → `orphan_cleanup`). It provides
incremental sync and automatic scoped hard deletes. Jobs missing from complete discovery,
and retained builds missing from complete `allBuilds`, are forgotten on the next sync.
Incomplete listings never authorize deletion. `confirm_deleted` is an optional extra guard.

Requires **Cognee >=1.6.5,<2** and Python **>=3.10,<3.14**. The factory refuses cores
lacking `DOCUMENT_SYNC_VERSION >= 2` or `PIPELINE_SCOPE_ATTR` before making requests.
The checked-in Ubuntu-generated lock selects Cognee 1.6.5. Real memory acceptance uses
Cognee's document, vector and graph stores with only LLM/embedding calls stubbed.

## What gets ingested

| Record | Id | Content |
| --- | --- | --- |
| Job | `job:<full name>` | description, parameter names/types/descriptions, enabled/disabled |
| Build | `build:<full name>#<number>` | result, causes, description, and for failed builds the tail of the console log |

Parameter **default values are never read** (they often hold secrets), and the raw `config.xml`
is not ingested. Console logs are fetched only for builds whose result is in `log_results`
(`FAILURE` by default), streamed, cut to the last `max_log_bytes` (64 KiB by default), and values
that match common credential patterns (`token=…`, `password: …`) are masked on a best-effort
basis. Build descriptions and cause descriptions are also masked, independently of `redact_logs`.
Job content is masked as a whole. **This is not general-purpose secret detection or a DLP
guarantee**: arbitrary secrets, encoded values, identifiers, URLs and values split at the log
boundary can remain. Review the trusted Jenkins scope and content before ingestion.

## Install

```bash
uv pip install cognee-community-connector-jenkins
# or, from this monorepo:
cd packages/connector/jenkins && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_jenkins import jenkins_source

await cognee.remember(
    jenkins_source(
        base_url="https://jenkins.example.com",
        username="you",
        api_token="...",
        job_names=["backend/main", "nightly-e2e"],  # omit to sync every visible job
    ),
    dataset_name="ci_history",
    primary_key="id",
    write_disposition="merge",  # required: incremental upsert by record id
    max_rows_per_table=0,  # unlimited: orphan-cleanup sees the whole corpus
)

answer = await cognee.search(
    query_text="Why did the backend/main build fail most recently?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["ci_history"],
)
```

Re-running `remember(...)` loads new completed builds and removes deleted jobs and rotated
builds after complete listings. Explicit `job_names` selects jobs to read; removing a name
from that selection retains its history. Only discovery can establish a job deletion.
Reuse a stable `resource_name` per installation and distinct names for different installations,
including when sharing a dataset. Cleanup is scoped to the source and dataset.

> **`write_disposition="merge"` is required** — the add pipeline defaults to `"replace"`, which
> would wipe the synced history on the second sync.

### Options

| Argument | Default | Meaning |
| --- | --- | --- |
| `job_names` | `None` | Full job names (`"folder/job"`). `None` discovers every job, descending into folders and multibranch projects. |
| `max_builds_per_job` | `20` | On a job's first sync, ingest only its most recent N builds (`0` = all). Later syncs fetch every new build. |
| `log_results` | `("FAILURE",)` | Build results whose console log is ingested, e.g. `("FAILURE", "UNSTABLE")`. |
| `max_log_bytes` | `65536` | Keep only the last N bytes of each ingested log. |
| `max_folder_depth` | `5` | How deep discovery descends into folders. |
| `redact_logs` | `True` | Best-effort masking of some credential formats in console logs. Not comprehensive DLP. |
| `resource_name` | `jenkins_records` | Stable DLT resource/scope name; choose a unique name per Jenkins source. |
| `service` / `session` | `None` | Requests-compatible client; aliases, pass only one. Caller owns injected clients; connector closes clients it creates. |
| `check_active` | `None` | Synchronous callback; raise to cancel. Checked before requests, retries, log chunks, rows and final state update. A request already in progress can last until its timeout. |
| `confirm_deleted` | `None` | `(full_name, build_number_or_None) -> bool`. Optional extra guard: when provided, return exactly `True` to allow a complete-list deletion. `None` as the second argument identifies a whole job. |

## How sync and forget-on-delete work

- **Bounded requests.** Build numbers are requested newest first using
  `allBuilds[number]{start,end}` in pages of at most 100. The initial sync can stop at
  `max_builds_per_job`; later runs stop early only when every retained build has been seen
  and the cursor has been reached. Missing retained builds require paging through the
  terminal empty page before rotation deletes are allowed. Short pages alone are never
  treated as terminal. Duplicate, out-of-order or oversized pages abort the run.
- **Incremental cursor.** Only completed builds above the cursor are fetched. A running
  build holds the cursor so it is retried. Missing `allBuilds` on the initial page falls
  back to `lastBuild` for ingestion and never authorizes build deletion; missing later
  pages, null/malformed lists and explicit partial signals abort without committing state.
- **Automatic deletion.** Discovery must return HTTP 200 for the root and every folder,
  without missing/malformed children, partial signals or a `max_folder_depth` cut. A missing
  job then emits a tombstone for itself and all its retained builds. A complete empty
  root or folder is valid, including deletion of the last job. Builds are deleted only
  after a complete `allBuilds` inventory. An optional `confirm_deleted(name, number)`
  callback can veto these deletes; it cannot authorize deletion from incomplete data.
  For a job deletion the callback receives `None` instead of a build number and guards
  the job and all its builds together. Cognee consumes the DLT tombstones through scoped
  document sync and removes corresponding documents, vectors and graph data.
- **Fail closed.** Non-200 responses, explicit job/build 404, partial discovery and depth
  cuts abort extraction. DLT persists source state only after a successful load. A configured
  job returning 404 keeps its old history. Jenkins can silently filter readable objects;
  HTTP 200 cannot universally detect such permission changes. Use a stable read scope and
  supply `confirm_deleted` when a stricter deletion policy is required.
- **HTTP behavior.** Retry only GET on transient responses, at most four retries. Numeric and HTTP
  date `Retry-After` values are capped at 30 seconds per wait, including cloned retry objects.
  Public HTTP failures omit request URLs and headers; raw exception chains are suppressed.
- **Statistics.** `resource.cognee_sync_stats` resets on each extraction. Integer `scanned` counts
  evaluated jobs and candidate builds; `skipped` counts unchanged jobs and running/already ingested
  candidates. `emitted` and `deleted` count yielded rows. `failed` counts aborted extractions;
  `failed_rate_limit` counts aborts on a final HTTP 429. These are extraction counters, not committed
  DLT loads or stored memory counts.

## Setup

1. In Jenkins, open your user (top-right) → **Security** → **API Token** → **Add new Token**.
   A read-only user with *Overall/Read* and *Job/Read* is enough; the connector only sends `GET`
   requests.
2. Pass `base_url`, `username` and `api_token` (sent as HTTP Basic auth), plus your `LLM_API_KEY`
   like any other cognee run.

To try it locally without a Jenkins server:

```bash
docker run --rm -p 8080:8080 jenkins/jenkins:lts-jdk21
```

## Testing

```bash
uv run --with pytest pytest tests/
```

The source tests use fake Jenkins and include real DLT SQLite merge and rollback regressions,
bounded 10,000-build history, malformed/capped listing, masking, cancellation and retries.
The fork CI matrix runs Python 3.10, 3.11 and 3.13, with real memory acceptance on 3.11.
They have **not run on the development Windows host**: enforced WDAC blocks its Cognee native
stack. Do not reroute around that policy. Windows checks remain static only.

`tests/test_remember_acceptance.py` contains real two-pass `remember()` assertions for documents,
vector payloads, graph entities, real vector search, repeat-sync model-call counts, overlapping
source IDs, alternating datasets and memory retention after failed extraction. `tests/remember_worker.py`
implements the real store readers and launches no paid models. On an approved non-Windows runner,
the pytest wrapper starts a fresh process with only explicit test settings and disposable paths.
It rejects unexpected inherited environment settings, disables dotenv loading, verifies provider
configuration and guards SQLite, graph, vector and DLT constructors against shared paths. It also
rejects symlink escapes, blocks Python network connections, requires the bundled graph extension,
and forbids global prune and remote graph-extension installation. Public hash-verified tokenizer
assets are provisioned in a credential-free process before the offline worker. Model stubs are
installed before engine setup. The JSON result file is written only after every memory assertion
succeeds and no caught or uncaught Python network attempt occurred.

The memory test skips by default and remains disabled on the WDAC-blocked Windows host, even with
the opt-in. The opt-in cannot bypass the isolation checks. Run on an approved non-Windows
runner after installation:

```bash
python -m pytest tests/ --run-jenkins-memory -q
```

The DLT failure regression additionally reopens the same disposable SQLite pipeline after partial
extraction fails and checks the persisted cursor, unchanged rows and successful retry. This
regression passed on all three Ubuntu Python versions. Follow the isolated
[CI workflow](https://github.com/ShenJun93/cognee-community/actions/workflows/jenkins-connector-ci.yml)
for the current memory acceptance result. A skip is not memory evidence, and passing fixture
deletion alone does not validate all incomplete-listing and isolation cases. No test or example
calls global prune.
