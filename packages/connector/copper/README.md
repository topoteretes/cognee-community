# cognee-community-connector-copper

A Copper CRM data-source connector for [cognee](https://github.com/topoteretes/cognee): sync
your companies, people, opportunities and their activity log (notes, calls, meetings) into
memory, so you can "ask my CRM".

It exposes a `dlt` resource you hand to `cognee.remember(...)`. That reuses cognee's existing
DLT ingestion path (`resolve_dlt_sources` → `ingest_dlt_source` → `orphan_cleanup`), so you get
**incremental re-sync** (only records whose content changed are processed again) and
**forget-on-delete** (records deleted in Copper are removed from memory on the next sync)
with no core changes.

## Requirements

- Python 3.10 to 3.14.
- cognee 1.6.3 or later (below 2), for table-scoped document cleanup.
- Any Copper plan with API access. A free trial works.

## Install

```bash
uv pip install cognee-community-connector-copper
# or, from this monorepo:
cd packages/connector/copper && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_copper import copper_source

await cognee.remember(
    copper_source(
        api_key="...",
        user_email="you@example.com",  # the user who generated the key
        resource_name="copper_sales",
    ),
    dataset_name="copper_crm",
    primary_key="id",
    write_disposition="merge",  # incremental upsert by record id
    max_rows_per_table=0,  # unlimited: orphan-cleanup sees the whole corpus
)

answer = await cognee.search(
    query_text="Which open opportunities have notes about blockers?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["copper_crm"],
)
```

Choose what to sync with `include=("companies", "people", "opportunities", "activities")`
(all four by default). Activities are synced only for the record kinds you include, so
`include` must name at least one of companies, people or opportunities. Running
`remember(...)` again with the same dataset syncs only records changed since the last run
and forgets records that were deleted. See `examples/example.py` for the full flow.

> **`write_disposition="merge"` is required.** The add pipeline defaults to `"replace"`,
> which would wipe the synced records on the second sync.

`resource_name` is required and names one Copper account's sync: it keys the sync state and
the cleanup of deleted records. Give every account its own name, even in one dataset. Two
accounts sharing a name would forget each other's records, so a run refuses state that was
written for another account. Pass `check_active` to let a host stop a sync whose access was
revoked; it is called before every request.

## What gets ingested

One document per record, with ids turned into names (owners, pipelines, stages, contact
types, sources, loss reasons, activity types, custom fields and their options):

- **Opportunities**: name, status, pipeline, stage, value, company, primary contact, owner,
  priority, close date, source, loss reason, tags, custom fields, details.
- **People**: name, job title, company, contact type, owner, tags, custom fields, details.
- **Companies**: name, email domain, contact type, owner, tags, custom fields, details.
- **Activities**: the text of notes, calls, meetings and other user-logged activity types,
  with their type, the company, person or opportunity they were logged on, who logged them
  and the date. Each links back to its parent record in the Copper app.

Each activity is its own document, so editing one note processes only that note again. Every
activity names the record it belongs to, and the graph links them through that name.
System activities (such as "stage changed") and disabled activity types are left out, and so
are activities on leads, projects and tasks. Email addresses, phone numbers, addresses and
social profiles of people and companies are left out on purpose.

## How sync + forget-on-delete work

Copper has no feed of deleted records, and its search filters on `minimum_modified_date` only
for some record types. So each run searches the selected records in full and emits only those
whose rendered text changed, using a fingerprint kept in dlt's per-resource state.

`minimum_modified_date` alone is not used as the cursor because it misses changes that matter
here: renaming an opportunity does not change the `date_modified` of its notes, but every note
document names its opportunity, so the note must be rendered again. Comparing the rendered
text catches that, and also custom field, stage and activity type renames, while records that
did not change are never processed again.

A record that stops showing up is checked once more by id before it is forgotten. A deleted
record (404), an activity whose type was disabled or whose parent kind was dropped from
`include`, or a record of a kind dropped from `include` is emitted with the `_deleted`
hard-delete marker. dlt drops it on `merge`, and cognee's `orphan_cleanup` removes it from the
graph, vector and relational stores. A record that is still there is kept. Keep
`resource_name` and the dataset fixed across runs.

A run never sleeps for long on Copper's rate limits. A `429` is waited out (with its
`Retry-After` when it is a minute or less), but one run waits at most two minutes in total on
retries. Past that, or once `max_requests` (1000 by default) is spent, the run stops cleanly.
It keeps its progress, reports it in `source.cognee_sync_stats`, and the next run resumes on
the page it stopped at. Deletions are only decided once a full cycle over every search has
finished. A record whose check fails is kept as it is, counted in `failed` and checked again
on the next run, and the rest of the sync goes on.

## Limitations

- Every cycle searches all selected records: one request per 200 records, plus one per
  re-checked record. 20,000 records cost about 100 requests per run, well inside Copper's
  limit of 180 requests a minute, but a sync every few minutes would add up.
- Copper search returns at most the first 100,000 records of a query. An account above that
  for one record type syncs those 100,000, never decides deletions for that type, and reports
  it as `failed_cap` in the sync stats.
- Leads, projects, tasks, files and emails are not synced.
- Only records the key's user can see are synced. When a record becomes hidden from that
  user, the re-check decides: a 404 forgets it, a 403 keeps it and counts it in `failed`.
- The dataset holds deal values, people's names and note text. With cognee's access control
  (on by default) only the user who ran the sync, and users granted permission on the dataset,
  can read it. Keep it in its own dataset and share it only with people allowed to see the CRM.

## Setup

1. In Copper open **Settings → Integrations → API Keys** and generate a key. The key has the
   same access as your user, so use a user that can see the records you want in memory.
2. Pass it as `api_key`, and the email of the user who generated it as `user_email`. Copper
   rejects a key sent with any other email (HTTP 401). The connector only searches and reads
   records. Set your `LLM_API_KEY` like any other cognee run.

## Testing

```bash
uv run --with pytest --with pytest-asyncio pytest tests/
```

The tests fake the Copper API (no live key) and include offline end-to-end runs through a real
`dlt` merge and through `cognee.add` and `cognify`, proving a deleted record is removed from
the graph, not only from staging.
