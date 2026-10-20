# cognee-community-connector-datadog

A Datadog data-source connector for [cognee](https://github.com/topoteretes/cognee): sync the
human-written side of your Datadog org (monitors and their runbooks, dashboards, notebooks and
postmortems, SLOs and incidents) into memory, so an agent can answer "what do we know about
checkout latency?" from what your team wrote down.

It exposes a `dlt` resource you hand to `cognee.remember(...)`, reusing cognee's existing DLT
ingestion path (`resolve_dlt_sources` → `ingest_dlt_source` → `orphan_cleanup`), so you get
**incremental re-sync** (only items whose content changed are re-processed) and
**forget-on-delete** (items deleted in Datadog are purged from memory on the next sync) with no
core changes.

## Requirements

- Python 3.10 to 3.14.
- cognee 1.6.3 or later (below 2), for table-scoped document cleanup.
- A Datadog API key and an application key. A read-only scoped application key is enough.

## Install

```bash
uv pip install cognee-community-connector-datadog
# or, from this monorepo:
cd packages/connector/datadog && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_datadog import datadog_source

await cognee.remember(
    datadog_source(
        api_key="...",
        app_key="...",
        site="datadoghq.eu",  # the site your org lives on, as in DD_SITE
        resource_name="datadog_prod",
    ),
    dataset_name="datadog",
    primary_key="id",
    write_disposition="merge",  # incremental upsert by item id
    max_rows_per_table=0,  # unlimited: orphan-cleanup sees the whole corpus
)

answer = await cognee.search(
    query_text="What is the runbook when checkout latency alerts, and which SLO does it affect?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["datadog"],
)
```

Choose what to sync with `include=("monitors", "dashboards", "notebooks", "slos", "incidents")`.
Re-running `remember(...)` with the same dataset syncs only items changed since the last run and
forgets items that were deleted. See `examples/example.py` for the full flow.

> **`write_disposition="merge"` is required**: the add pipeline defaults to `"replace"`,
> which would wipe the synced items on the second sync.

`site` picks the region. Datadog orgs live on one site and keys only work there:

| `site` | Region |
|---|---|
| `datadoghq.com` (default) | US1 |
| `us3.datadoghq.com` | US3 |
| `us5.datadoghq.com` | US5 |
| `datadoghq.eu` | EU1 |
| `ap1.datadoghq.com` | AP1 |
| `ap2.datadoghq.com` | AP2 |
| `ddog-gov.com` | US1-FED |

Any other value is refused before a request is made, so the keys are never sent to an
unknown host.

`resource_name` is required and names one Datadog org's sync: it keys the sync state and the
cleanup of deleted items. Give every org its own name, even in one dataset; two orgs sharing a
name would forget each other's items, so a run refuses state that was written for another site.
Pass `check_active` to let a host stop a sync whose access was revoked; it is called before
every request.

## What gets ingested

Only what people wrote. Metrics, logs, traces and events are never read: they are
high-volume time series that a knowledge graph can't answer much from.

- **Monitors**: name, type, query, thresholds, priority, tags and the message, which is often
  the runbook. The live state (OK, Alert, ...) is left out, so an alert firing does not make
  a new document.
- **Dashboards**: title, description, tags, the titles of the widgets (nested groups
  included) and the text of note widgets.
- **Notebooks**: name, type (postmortem, runbook, investigation, ...), status and the markdown
  cells, plus the titles of graph cells. Templates are left out.
- **SLOs**: name, description, type, targets per timeframe, the good/total event queries, the
  monitors it is built on (by name) and tags.
- **Incidents**: title, severity, state, customer impact, declared and resolved dates, and
  every filled-in field, your org's own fields included (summary, root cause, detection
  method, services, teams, ...). Resolved incidents stay: they hold the postmortem knowledge.
  Test incidents are left out.

## How sync + forget-on-delete work

No Datadog list endpoint can filter by modified time, so each run lists every selected kind in
full (a cheap call per 100 items) and emits only items whose rendered text changed, using a
fingerprint kept in dlt's per-resource state.

Lists don't carry a dashboard's widgets or a notebook's cells, so those two are fetched one by
one, and only when the `modified` time in the list moved. A dashboard saved without a change
that shows in its text is not emitted again.

An item missing from a listing that finished is checked once more with a GET before it is
forgotten, so a list that shifted while it was paged can't forget a live item. Monitors, SLOs,
notebooks and incidents are hard deleted, so the GET answers 404. A deleted dashboard goes to a
restorable trash that the list leaves out and that also answers 404, so it is forgotten the
same way. A restored dashboard keeps its old `modified` time, but it was forgotten, so it is
read and ingested again. (Checked live on a US1 org: create, edit, delete, trash and restore.) A forgotten item is emitted with the `_deleted` hard-delete marker, dlt drops it
on `merge`, and cognee's `orphan_cleanup` removes it from the graph + vector + relational
stores. Deselecting a kind in `include` forgets its items too. Keep `resource_name` and the
dataset fixed across runs.

## Rate limits

Datadog limits each endpoint on its own, from 60 requests a minute (the dashboard list) to
1,000 per 10 seconds (the monitor and SLO lists). A run never sleeps on them: it reads the
`X-RateLimit-Remaining` header of every answer and stops an endpoint when fewer than 5 requests
are left in its window.

- A list that stops early stops only its kind; the other kinds go on, and nothing of that kind
  is forgotten in that run, since an unfinished list proves nothing.
- A dashboard or notebook fetch that stops early is deferred: the item keeps its old state and
  is read on the next run. Deletions are still applied.
- `max_requests` (500 by default) caps a whole run. A large org finishes over several runs.

Network errors and 5xx answers are retried twice. A list that still fails is treated like one
that stopped early: that kind is skipped for the run and nothing of it is forgotten. Only a
rejected API key (HTTP 401) fails the whole run.

Progress is reported in `source.cognee_sync_stats` (`scanned`, `skipped`, `deferred`, `deleted`,
`failed`, and a `failed_<reason>_<kind>` entry, reason `rate_limit`, `access` or `error`, for a
kind that stopped early or was refused).

## Limitations

- An application key without a kind's read scope makes that kind fail with HTTP 403. The run
  skips it, logs the missing scope and goes on; a run where every kind is refused raises
  `DatadogAccessError`, which usually means the keys belong to another site.
- Incident timelines, monitor alert history and dashboard tabs are not ingested. Alert history
  is an event stream (one entry per state change), the same high-volume kind of data as
  metrics and logs; a monitor's definition and runbook are what an agent needs to reason with.
- The dataset holds what your team wrote in Datadog, which can include internal hostnames and
  on-call handles. With cognee's access control (on by default) only the user who ran the sync,
  and users granted permission on the dataset, can read it.

## Setup

1. In Datadog, go to **Organization Settings → API Keys** and create (or copy) an API key.
2. Under **Organization Settings → Application Keys**, create an application key. To keep it
   read-only, give it only these scopes: `monitors_read`, `dashboards_read`, `notebooks_read`,
   `slos_read` and `incident_read`.
3. Note your site: it is in the address bar when you are logged in (`app.datadoghq.eu` → EU1,
   `us5.datadoghq.com` → US5, ...).
4. Export them, plus your `LLM_API_KEY` like any other cognee run:

   ```bash
   export DD_API_KEY="…"
   export DD_APP_KEY="…"
   export DD_SITE="datadoghq.com"
   ```

## Testing

```bash
uv run --with pytest --with pytest-asyncio pytest tests/
```

The tests fake the Datadog API at the HTTP layer (no live key), so dlt's real paginators run
against it, and include offline end-to-end runs through a real `dlt` merge and through
`cognee.add` and `cognify`, proving a deleted monitor is removed from the graph, not only from
staging.
