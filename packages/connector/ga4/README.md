# cognee-community-connector-ga4

A Google Analytics 4 data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync a property's custom dimensions, custom metrics, key events, audience definitions and
reporting annotations into memory — "what does my analytics setup mean?".

It exposes a `dlt` resource you hand to `cognee.remember(...)`, reusing cognee's existing DLT
ingestion path (`resolve_dlt_sources` → `ingest_dlt_source` → `orphan_cleanup`) — so you get
**incremental re-sync** (only definitions whose content changed are re-processed) and
**forget-on-delete** (definitions archived or deleted in GA4 are purged from memory on the next
sync) with no core changes.

## Requirements

- cognee 1.6.1 or later (pinned to `1.6.4`), for table-scoped document cleanup.
- A Google Cloud service account with Viewer access to the GA4 property, and the Google
  Analytics Admin API enabled in its project. The connector only asks for the
  `analytics.readonly` scope. A standard (free) property is enough: every listing used here was
  verified on one with a Viewer service account, including that archiving a custom dimension or
  audience in the GA4 interface drops it from the listing.

## Install

```bash
uv pip install cognee-community-connector-ga4
# or, from this monorepo:
cd packages/connector/ga4 && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_ga4 import ga4_source

await cognee.remember(
    ga4_source("123456789", credentials_path="service-account.json"),
    dataset_name="ga4_analytics",
    primary_key="id",
    write_disposition="merge",  # incremental upsert by resource name
    max_rows_per_table=0,  # unlimited: orphan-cleanup sees the whole corpus
)

answer = await cognee.search(
    query_text="Which audiences and key events track purchases, and how are they defined?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["ga4_analytics"],
)
```

`property_id` is the numeric id under **Admin → Property details**, not the `G-` measurement id.
Without `credentials_path` the connector uses Application Default Credentials, so
`GOOGLE_APPLICATION_CREDENTIALS` pointing at the key file works too. Choose what to sync with
`include=("custom_dimensions", "custom_metrics", "key_events", "audiences", "annotations")`.
Re-running `remember(...)` with the same dataset syncs only definitions changed since the last
run and forgets the ones that were removed. See `examples/example.py` for the full flow.

> **`write_disposition="merge"` is required** — the add pipeline defaults to `"replace"`,
> which would wipe the synced definitions on the second sync.

## What gets ingested

Only the setup of the property and the context your team wrote around it. The Data API is not
called, so no report numbers reach the graph.

- **Custom dimensions**: display name, parameter name, scope (event, user or item), whether it is
  excluded from ads personalization, and its description.
- **Custom metrics**: display name, parameter name, scope, unit and restricted data types.
- **Key events**: event name, how it is counted (once per event or per session), its default
  value and whether it is a custom or a Google Analytics default event.
- **Audiences**: name, description, membership duration, exclusion mode, the event logged when
  a user joins, and the definition rules. Default audiences such as "All Users" come back without
  rules, so they carry their name and description only.
- **Annotations**: title, date or date range, description, and whether Google Analytics added
  it — the place teams record why a metric moved.

Each definition is one document, keyed by its resource name
(`properties/<id>/audiences/<id>`, ...). Creation times are left out: they never change the
meaning of a definition.

Saved reports and Explorations are not ingested because no Google Analytics API exposes them:
the Data API only runs reports. Audiences and annotations are read from the Admin API's
`v1alpha` version, the only one that has them, so those two kinds can change with that API; the
other three use `v1beta`.

## How sync + forget-on-delete work

The Admin API has no change feed for these definitions and they carry no update time, so each
run lists the selected kinds in full (pages of 200) and emits only the definitions whose
rendered text changed, using a fingerprint kept in dlt's per-resource state. A property's whole
setup is a few hundred definitions at most, so a run is a handful of requests.

A definition that a complete listing no longer holds — an archived custom dimension, custom
metric or audience, a deleted key event or annotation — is emitted with the `_deleted`
hard-delete marker, dlt drops it on `merge`, and cognee's `orphan_cleanup` removes it from the
graph + vector + relational stores. Deselecting a kind in `include` forgets its definitions too.

The account change history (`searchChangeHistoryEvents`) is not used: it needs the
`analytics.edit` scope, and a read-only connector should not ask for write access.

A run never sleeps on the Admin API quota. When it answers 429 the run stops cleanly, keeps its
progress and reports it in `source.cognee_sync_stats` (`failed_rate_limit`); the next run
resumes. Server errors are retried with a short backoff. A listing that is interrupted, or comes
back in an unexpected shape, never deletes anything.

Credentials that are missing or rejected, a service account without access to the property and
an Admin API that is not enabled raise a clear error before anything is synced. Losing access in
the middle of a run stops it like a rate limit (`failed_access`), keeping every document.

Keep `resource_name` (default `ga4_<property id>`) and the dataset fixed across runs. Each
property gets its own name by default, so several properties can share one dataset.

## Setup

1. In the [Google Cloud console](https://console.cloud.google.com/), pick or create a project,
   enable the **Google Analytics Admin API**, then create a service account under
   **IAM & Admin → Service Accounts** and add a JSON key. Keep the key file private.
2. In Google Analytics open **Admin → Property access management**, add the service account's
   email (`...@<project>.iam.gserviceaccount.com`) and give it the **Viewer** role.
3. Pass the key file as `credentials_path` (or set `GOOGLE_APPLICATION_CREDENTIALS`) and the
   property id, plus your `LLM_API_KEY` like any other cognee run.

## Testing

```bash
uv run --with pytest --with pytest-asyncio pytest tests/
```

The tests fake the Admin API (no live credentials; the key file is generated per run) and include
offline end-to-end runs through a real `dlt` merge and through `cognee.add` and `cognify`, proving
an archived audience is removed from the graph, not only from staging.
