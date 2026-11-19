# cognee-community-connector-sonarqube

A SonarQube data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your code-quality state into memory — "ask my code quality".

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`. Issues,
security hotspots, and quality-gate outcomes are rendered to markdown documents and
ingested as **normal documents** (they flow through cognee's cognify entity-extraction
pipeline, not the deterministic dlt-row path), via cognee's document-mode marker. Works
with SonarCloud too — just point `base_url` at `https://sonarcloud.io`.

## Install

```bash
uv pip install cognee-community-connector-sonarqube
# or, from this monorepo:
cd packages/connector/sonarqube && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_sonarqube import sonarqube_source

await cognee.remember(
    sonarqube_source(
        base_url="https://sonarqube.example.com",
        token="...",  # or SONARQUBE_TOKEN
        project_keys=["org_repo"],  # None = every visible project
        min_severity="MAJOR",  # skip INFO/MINOR noise (recommended)
    ),
    dataset_name="code_quality",
    primary_key="id",
    write_disposition="merge",  # incremental upsert by issue/hotspot key
    max_rows_per_table=0,  # unlimited read-back so deletions reconcile fully
)

answer = await cognee.search(
    query_text="What is blocking the quality gate on org_repo?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["code_quality"],
)
```

See `examples/example.py` for the full flow.

## How sync + forget-on-delete work

**Auth:** a SonarQube user token (**Administration → Security → Users → Generate token**,
or your account's *My Account → Security* page), sent as `Authorization: Bearer` on every
request. Pass it via `token=` or the `SONARQUBE_TOKEN` environment variable. Every
request is a `GET`.

**What is ingested:** one document per issue (message, rule, severity, type, status,
component, tags — deep-linked to the SonarQube web UI), one per security hotspot, and
one per project carrying its quality-gate outcome and last analysis date.

**Incremental sync** follows the issue spec: `createdAfter` on the issues search. Each
run fetches only unresolved issues created since the highest `creationDate` stored for
the project (cursor persisted in dlt's per-resource state, so re-running `remember`
resumes where it left off). Documents are content-hashed, so boundary re-reads are
no-ops downstream. The `min_severity` filter bounds large projects to unresolved issues
at/above a threshold (`INFO` < `MINOR` < `MAJOR` < `CRITICAL` < `BLOCKER`).

**Forget-on-delete:** each run does a keys-only sweep of every fetched project's
*unresolved* issues — a known issue that left the unresolved set (resolved **or**
deleted upstream) is emitted with an `_deleted` hard-delete marker. Hotspots are swept
in full each run: hotspots marked `REVIEWED` or removed are tombstoned. Projects that
vanish from the project listing (or from `project_keys`) tombstone all their documents,
including the quality-gate document. dlt removes marked rows on merge, and cognee's
existing `orphan_cleanup` purges them from the graph, vector, and relational stores.

**Failure posture:** a project that fails to fetch is skipped for the run (with a
warning) — its documents are never tombstoned on unseen evidence, and its cursor is
kept. A project listing failure skips the whole sync rather than masquerading as "all
projects deleted".

## Limitations

- In-place issue edits that don't resolve the issue (e.g. a severity bump) are not
  picked up until its resolution: `createdAfter` — the issue's own cursor design — cannot
  see them, and SonarQube's search API has no `updatedAfter` filter. Resolutions and
  deletions *are* caught by the unresolved sweep.
- Syncing several SonarQube servers into one dataset? Give each `sonarqube_source(...)`
  call its own `resource_name` so each keeps its own incremental state and staging table.

## Testing

```bash
uv run pytest tests/
```

The tests mock the SonarQube Web API (no network, no credentials) and cover document
rendering, the `createdAfter` cursor, severity filtering, sweep-based
forget-on-delete (resolved issues, reviewed/removed hotspots, vanished projects,
removed quality gates), failure handling, and an end-to-end forget-on-delete through a
real dlt merge.
