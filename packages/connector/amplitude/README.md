# cognee-community-connector-amplitude

An Amplitude data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your event taxonomy, cohort definitions, chart annotations and saved charts into memory —
"ask my analytics".

It exposes a `dlt` resource you hand to `cognee.remember(...)`, reusing cognee's existing DLT
ingestion path (`resolve_dlt_sources` → `ingest_dlt_source` → `orphan_cleanup`) — so you get
**incremental re-sync** (only records whose content changed are re-processed) and
**forget-on-delete** (records deleted or archived in Amplitude are purged from memory on the next
sync) with no core changes.

## Requirements

- cognee 1.6.1 or later (pinned to `1.6.3`), for table-scoped document cleanup.
- An Amplitude project's API key and secret key. The Starter (free) plan is enough: the
  taxonomy, cohort, annotation and chart endpoints were all verified there.

## Install

```bash
uv pip install cognee-community-connector-amplitude
# or, from this monorepo:
cd packages/connector/amplitude && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_amplitude import amplitude_source

await cognee.remember(
    amplitude_source(api_key="...", secret_key="..."),
    dataset_name="amplitude_analytics",
    primary_key="id",
    write_disposition="merge",  # incremental upsert by record id
    max_rows_per_table=0,  # unlimited: orphan-cleanup sees the whole corpus
)

answer = await cognee.search(
    query_text="Which events track checkout, and what do their properties mean?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["amplitude_analytics"],
)
```

Choose what to sync with `include=("events", "user_properties", "cohorts", "annotations")`, add
saved charts with `chart_ids=["abc123"]`, pass `region="eu"` for a project in the EU data region,
and `include_archived=True` to keep archived cohorts. Re-running `remember(...)` with the same
dataset syncs only records changed since the last run and forgets records that were deleted. See
`examples/example.py` for the full flow.

> **`write_disposition="merge"` is required** — the add pipeline defaults to `"replace"`,
> which would wipe the synced records on the second sync.

## What gets ingested

Only definitions and the context your team wrote around them. Raw events are never read: the
Export API is not called, so event volume has no effect on the ingest budget.

- **Events**: one document per event type, with its display name, category, description, owner,
  tags and its properties (type, allowed values, required, description).
- **User properties**: type, allowed values and description. Amplitude's built-in properties
  (`user_id`, `device_id`, ...) are left out unless you documented them.
- **Cohorts**: name, description, owners, type and the definition rules.
- **Annotations**: label, dates, category, details and the chart they are pinned to — the place
  teams record why a metric moved.
- **Charts** (opt-in): the title, measure and events of each saved chart named in `chart_ids`.
  The id is the last part of a chart's URL (`.../chart/abc123`).

Cohort sizes, compute times, view counts, event volumes and chart results are left out on
purpose: they change without anyone editing anything.

Charts are opt-in by id because, with an API key and secret key, Amplitude has no endpoint that
lists saved charts. A chart is read from the header of its CSV export; that request runs the
chart's query and counts toward the Dashboard REST API's cost limits, so name the charts that
matter. Amplitude's newer Developer API does list charts, but it needs a personal access token
instead of the project keys.

## How sync + forget-on-delete work

Amplitude has no change feed for this metadata, so each run lists the selected kinds and emits
only the records whose rendered text changed, using a fingerprint kept in dlt's per-resource
state. The Export API's date window is not used as a cursor, because it only covers raw events.
Listing is cheap: one request per kind, plus one per event type for its properties and one per
named chart.

A record that a complete listing no longer holds — a deleted event, property or annotation, an
archived or deleted cohort, a chart Amplitude answers 404 for — is emitted with the `_deleted`
hard-delete marker, dlt drops it on `merge`, and cognee's `orphan_cleanup` removes it from the
graph + vector + relational stores. Deselecting a kind in `include`, or dropping an id from
`chart_ids`, forgets those records too. Keep `resource_name` (default `"amplitude"`) and the
dataset fixed across runs.

A run never sleeps on Amplitude's rate limits. When a 429 or `max_requests` is reached it stops
cleanly, keeps its progress and reports it in `source.cognee_sync_stats`; the next run resumes.
An interrupted listing never deletes anything.

If the plan cannot read one kind (a 403 that is not about the credentials), that kind is skipped
with a warning and counted in `cognee_sync_stats["no_access"]`; its documents stay and the other
kinds still sync. A wrong key, secret or region also answers 403 in Amplitude, and that one fails
the run.

## Setup

1. In Amplitude open **Settings → Projects**, pick the project and stay on the **General** tab.
   Copy the **API Key**, then select **Manage** next to **Secret Key** and generate one.
   Amplitude shows a secret key only once, and only Admins and Managers can generate it.
2. Pass them as `api_key` and `secret_key` (read-only: the connector only sends `GET` requests),
   plus your `LLM_API_KEY` like any other cognee run.

## Testing

```bash
uv run --with pytest --with pytest-asyncio pytest tests/
```

The tests fake the Amplitude API (no live keys) and include offline end-to-end runs through a
real `dlt` merge and through `cognee.add` and `cognify`, proving an archived cohort is removed
from the graph, not only from staging.
