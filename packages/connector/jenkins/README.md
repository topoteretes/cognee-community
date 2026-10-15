# cognee-community-connector-jenkins

A Jenkins data-source connector for [cognee](https://github.com/topoteretes/cognee): sync your
CI history into memory and ask questions like *"why did the nightly build fail last week?"* or
*"which jobs started failing after the dependency bump?"*.

It exposes a `dlt` resource you hand to `cognee.remember(...)`, reusing cognee's existing DLT
ingestion path (`resolve_dlt_sources` → `ingest_dlt_source` → `orphan_cleanup`). You get
**incremental sync** (only builds newer than the last run are fetched) and **forget-on-delete**
(deleted jobs and discarded builds are removed from memory on the next sync) with no core changes.

## What gets ingested

| Record | Id | Content |
| --- | --- | --- |
| Job | `job:<full name>` | description, parameter names/types/descriptions, enabled/disabled, last build number |
| Build | `build:<full name>#<number>` | result, timestamp, duration, causes, description, and for failed builds the tail of the console log |

Parameter **default values are never read** (they often hold secrets), and the raw `config.xml`
is not ingested. Console logs are fetched only for builds whose result is in `log_results`
(`FAILURE` by default), streamed, cut to the last `max_log_bytes` (64 KiB by default), and values
that look like credentials (`token=…`, `password: …`) are masked.

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

Re-running `remember(...)` with the same dataset fetches only new builds and forgets what was
deleted. See `examples/example.py` for the full flow.

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
| `redact_logs` | `True` | Mask credential-like values in ingested logs. |

## How sync and forget-on-delete work

- **Bounded requests.** Every JSON call names its fields with `tree=`, so a large controller never
  returns its whole object graph (`depth=` can produce enormous payloads).
- **Incremental cursor.** For each job the highest fully ingested build number is kept in dlt's
  per-resource state. A run reads the job's build numbers and fetches only builds above it. A build
  that is still running is skipped and holds the cursor back, so it is ingested once it finishes.
- **Forget-on-delete.** Jenkins has no deletion feed, so each run compares the current jobs and
  build numbers with the previous run. A deleted job is emitted as a hard delete together with all
  of its builds; builds that Jenkins discarded (log rotation, "discard old builds", manual delete)
  are hard-deleted too. dlt drops those rows on `merge` and cognee's `orphan_cleanup` removes them
  from the graph, vector and relational stores. Removing a job from `job_names` forgets it as well.
- **Fail closed.** A sweep that returns no jobs while jobs were known is treated as a broken listing
  (wrong folder, lost permissions), not as "everything was deleted": nothing is forgotten and the
  state is kept. Any HTTP error aborts the run before dlt saves the state, so the next run retries
  from the last good point.

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

The tests mock the Jenkins JSON API (no server or token needed) and include an offline end-to-end
run that drives the source through a real `dlt` merge into SQLite, proving that new builds are
added incrementally and that deleted jobs and discarded builds are physically removed.
