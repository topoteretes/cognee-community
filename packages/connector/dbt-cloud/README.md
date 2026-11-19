# cognee-community-connector-dbt-cloud

A dbt Cloud (now also branded "dbt platform") data-source connector for
[cognee](https://github.com/topoteretes/cognee): sync model documentation, lineage, and job
run outcomes into memory — "ask my dbt project".

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`. Model/source/
exposure/metric definitions and run outcomes are ingested as **normal documents** (they flow
through cognee's cognify entity-extraction pipeline, not the deterministic dlt-row path), via
cognee's document-mode marker.

## Requirements

This connector requires a cognee release that ships "document-mode" — i.e.
`cognee.tasks.ingestion.dlt_utils.DOCUMENT_SOURCE_ATTR` and the `resolve_dlt_sources` routing
that reads it. The `cognee==1.4.0` pin in `pyproject.toml` is the first release that includes it.

## Install

```bash
uv pip install cognee-community-connector-dbt-cloud
# or, from this monorepo:
cd packages/connector/dbt-cloud && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_dbt_cloud import dbt_cloud_source

await cognee.remember(
    dbt_cloud_source(
        account_id=12345,
        environment_ids=[67890],  # or project_ids=[...] / job_ids=[...]
    ),
    dataset_name="dbt_cloud",
    primary_key="id",
    write_disposition="merge",
    max_rows_per_table=0,
)

# Re-run any time (e.g. on a schedule) to pick up new runs and forget models
# or run outcomes that no longer exist upstream -- see "How sync works" below.

answer = await cognee.search(
    query_text="What feeds the orders model, and did its last run pass?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["dbt_cloud"],
)
```

Selection is **required**: pass at least one of `environment_ids=[...]`, `project_ids=[...]`, or
`job_ids=[...]`. `environment_ids` takes precedence when combined with `project_ids` (which then
filters that environment's jobs locally); `job_ids` bypasses discovery entirely and syncs exactly
those jobs. See `examples/example.py` for the full flow.

## Auth

Use a **personal access token** or a **service account token** — pass `api_token=` or set
`DBT_CLOUD_API_TOKEN`. The deprecated "user API key" scheme is not supported.

**API access itself requires a plan that includes it.** As of this connector's research
(October 2026), the free Developer plan does **not** include Administrative API access; Starter,
Enterprise, and Enterprise+ do. A **14-day free trial of Starter** (no credit card required) is
the fastest way to get a real account with API access for testing — see
[getdbt.com/pricing](https://www.getdbt.com/pricing) and
[docs.getdbt.com/docs/dbt-apis/admin-api](https://docs.getdbt.com/docs/dbt-apis/admin-api). The
trial auto-downgrades to the API-less Developer plan after 14 days if not upgraded.

### Minimum permissions

The token's user/service-account needs **read-only** access to Jobs, Runs, and Artifacts:

- **Enterprise**: the `Read-Only` or `Account Viewer` permission set (or `Job Viewer` scoped to
  the relevant project). Avoid `Analyst read`, which explicitly excludes job/run access. See
  [docs.getdbt.com/docs/platform/manage-access/enterprise-permissions](https://docs.getdbt.com/docs/platform/manage-access/enterprise-permissions).
- **Starter/Team**: assign the `Read-only` license type to the user who creates the personal
  access token. See
  [docs.getdbt.com/docs/platform/manage-access/self-service-permissions](https://docs.getdbt.com/docs/platform/manage-access/self-service-permissions).
- Token creation itself: [docs.getdbt.com/docs/dbt-apis/user-tokens](https://docs.getdbt.com/docs/dbt-apis/user-tokens)
  (personal access tokens) or
  [docs.getdbt.com/docs/dbt-apis/service-tokens](https://docs.getdbt.com/docs/dbt-apis/service-tokens)
  (service account tokens).

### Host / region

Pass `host=` (or set `DBT_CLOUD_HOST`) to your account's access URL, found in **Account settings
→ Account information** — e.g. `abc123.us1.dbt.com`. With or without an `https://` scheme and a
trailing slash both work; `http://` and any host containing a path/query/credentials are
rejected. The legacy global host `cloud.getdbt.com` (this connector's default when `host` is
omitted) is **scheduled for deprecation on 2027-02-03** — see
[docs.getdbt.com/docs/platform/about-platform/account-url-migration](https://docs.getdbt.com/docs/platform/about-platform/account-url-migration).
There is no API endpoint that tells a token which host/region it belongs to; you must look it up
in the UI.

## What is ingested

**Definitions** (one document per node): models, sources, seeds, snapshots, exposures, and
metrics, chosen via `resource_types=(...)` (default: all six). For each, the manifest's
description, materialization, database/schema/location, path, tags, selected metadata, columns
(with types from `catalog.json` when `include_catalog=True`, the default), attached generic
tests, and **lineage written as text** (upstream/downstream by name and unique id, so cognee's
graph extraction connects nodes across documents). Raw/compiled SQL is **off by default**
(`include_sql=True` to opt in — it can be large and occasionally sensitive). Macros, analyses,
docs blocks, and selectors are never ingested. Nodes from installed packages are excluded by
default (`include_packages=True` to opt in).

**Run outcomes** (one document per finished run, `include_run_outcomes=True` by default): job
and environment identity, status, git branch/sha, finish time, the run's status message, result
counts by status, and the failing/erroring/warning nodes (capped, with trimmed messages) from
`run_results.json`. Limited to each job's newest `max_runs_per_job` finished runs (default 50).

**CI/merge-triggered jobs are excluded by default** (`include_ci_jobs=True` to opt in) — they run
against ephemeral branches/PRs, not a stable project definition.

## How sync + forget-on-delete work

Each selected **job** gets exactly **one `/runs/` listing per sync** (newest-first, `state=active`
so deleted runs are excluded) — that single scan serves two purposes at once: picking the
environment's manifest source and windowing that job's run-outcome documents. Progress is
remembered across syncs via `dlt`'s resource state (per job: a `cursor` + the finished-run ids
currently represented in memory; per environment: the manifest-source run id + the node ids
currently represented in memory).

Every sync is either a **full pass** or an **incremental pass**:

- The **first** sync, any sync whose `account_id`/`project_ids`/`environment_ids`/`job_ids`/
  `resource_types`/`include_*`/`max_runs_per_job` selection differs from the last sync's (a config
  change always forces a full pass, even if nothing else changed), and every `full_sync_every`th
  sync (default 10; `full_sync_every=1` forces a full pass every time) are **full**: every job's
  run history is scanned from the start, every environment's manifest is re-fetched and
  re-rendered (even if the same run is still the latest success — a config change needs a fresh
  render), and jobs/environments that have disappeared from the current selection are detected
  and their previously-synced documents tombstoned.
- All other syncs are **incremental**: each job's run listing stops once it reaches runs already
  seen (by `cursor`), a job's new finished runs only trigger a run-outcome fetch for those new
  runs (not the whole window), and an environment's manifest is only re-fetched when the pass
  finds a **new** latest successful run for it.

**Forget-on-delete** uses `write_disposition="merge"` with a `_deleted` hard-delete column (pass
`write_disposition="merge", max_rows_per_table=0` to `cognee.add`/`cognee.remember` as shown
above): this connector never deletes rows itself, it emits a normal row for anything still live
and a `{"id": ..., "_deleted": True}` tombstone for anything that should be forgotten, and `dlt`
physically removes the matching row on merge. Tombstones are emitted for:

- A model/source/seed/snapshot/exposure/metric that no longer appears in the current manifest
  (removed from the dbt project, or newly excluded by a narrower `resource_types`/
  `include_packages` config).
- A run-outcome document whose run has slid out of the `max_runs_per_job` window, or whose run
  itself was deleted in dbt Cloud (confirmed via the `state=active` filter no longer returning
  it), or that is no longer wanted because `include_run_outcomes` was turned off.
- All of a job's or environment's previously-synced documents, when that job/environment is no
  longer resolved by the current selection on a full pass (e.g. the job was deleted, archived, or
  fell outside a narrowed `project_ids`/`environment_ids`).

Several guards exist specifically so a **transient or wrongly-scoped empty API response is never
mistaken for "everything was deleted"**: an environment with zero successful runs keeps its last
known definitions (warns, does not wipe); a job's full-pass run listing coming back completely
empty when runs were previously recorded keeps that job's state untouched (warns, does not treat
it as "all runs deleted"); vanished-job/environment detection on a full pass is skipped entirely
if the *current* job resolution itself comes back with zero jobs (far more likely a transient API
or scoping problem than every selected job vanishing at once).

Other behavior:

- Artifacts are capped at `max_artifact_mb` (default 200) to bound memory use; dbt Cloud retains
  run history metadata for **365 days** — see
  [docs.getdbt.com/docs/deploy/run-visibility](https://docs.getdbt.com/docs/deploy/run-visibility).
- Rate limits: the Administrative API allows **5,000 requests/minute**; a `429` triggers the
  documented flat **5-minute cooldown** before retrying (at most twice).
- **Each sync is all-or-nothing.** A network failure, an exhausted retry budget, a 404 on an
  explicitly configured job id, or an artifact exceeding `max_artifact_mb` aborts the whole run
  with an error before any state is committed for that run — nothing is partially applied, and
  `dlt`'s own pipeline-state rollback is a second, independent safety net on top of this
  connector's own commit-after-success discipline.

## Setup

1. Start a dbt Cloud/platform trial or use an existing Starter/Enterprise account; note your
   account id and access-URL host from **Account settings**.
2. Create a personal access token with the minimum permissions above.
3. Export the credential and your LLM key, then run the example:

   ```bash
   export DBT_CLOUD_ACCOUNT_ID="12345"
   export DBT_CLOUD_API_TOKEN="..."
   export DBT_CLOUD_HOST="abc123.us1.dbt.com"
   export DBT_CLOUD_ENVIRONMENT_IDS="67890"
   export LLM_API_KEY="sk-..."
   uv run python examples/example.py
   ```

## Testing

```bash
uv run pytest tests/
```

The tests mock the dbt Cloud API (no live credential) and cover factory validation and host
normalization, pagination, retry/error handling, job resolution (explicit ids, per-environment/
per-project listing, CI-job exclusion), the incremental run-scan and full/incremental-pass
decision logic (config-hash change detection, `full_sync_every` counting, sort-order and
mixed-UTC-offset defensiveness), definition and run-outcome sync (new/changed/removed nodes,
sliding run-outcome windows, vanished-job/environment reconciliation, every empty-sweep guard),
definition/run-outcome rendering (resource-type/package filtering, tests, lineage, catalog join,
defensive handling of missing/unexpected manifest fields), mid-run-exception state safety, a full
merge + forget-on-delete sync through a real `dlt` pipeline run, and a graph-level forget test
(`tests/test_dbt_cloud_forget.py`) that cognifies with LLM/embeddings mocked and asserts a removed
model's extracted entity is actually gone from the graph, not just its `Data` record.
