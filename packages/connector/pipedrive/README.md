# cognee-community-connector-pipedrive

A Pipedrive data-source connector for [cognee](https://github.com/topoteretes/cognee): sync your
deals, people, organizations, activities and notes into memory — "ask my CRM".

It exposes a `dlt` resource you hand to `cognee.remember(...)`, reusing cognee's existing DLT
ingestion path (`resolve_dlt_sources` → `ingest_dlt_source` → `orphan_cleanup`) — so you get
**incremental re-sync** (only records whose content changed are re-processed) and
**forget-on-delete** (records deleted in Pipedrive are purged from memory on the next sync) with
no core changes.

## Requirements

- Python 3.10 to 3.14.
- cognee 1.6.3 or later (below 2), for table-scoped document cleanup.
- Any Pipedrive plan. A free [developer sandbox](https://developers.pipedrive.com/) works.

## Install

```bash
uv pip install cognee-community-connector-pipedrive
# or, from this monorepo:
cd packages/connector/pipedrive && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_pipedrive import pipedrive_source

await cognee.remember(
    pipedrive_source(api_token="...", resource_name="pipedrive_sales"),
    dataset_name="pipedrive_crm",
    primary_key="id",
    write_disposition="merge",  # incremental upsert by record id
    max_rows_per_table=0,  # unlimited: orphan-cleanup sees the whole corpus
)

answer = await cognee.search(
    query_text="Which open deals have notes about blockers?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["pipedrive_crm"],
)
```

Choose what to sync with
`include=("organizations", "persons", "deals", "activities", "notes")` (all five by default).
Archived deals are synced too; pass `include_archived_deals=False` to leave them out, which
also forgets deals once they get archived. Re-running `remember(...)` with the same dataset
syncs only records changed since the last run and forgets records that were deleted. See
`examples/example.py` for the full flow.

> **`write_disposition="merge"` is required** — the add pipeline defaults to `"replace"`,
> which would wipe the synced records on the second sync.

`resource_name` is required and names one Pipedrive company's sync: it keys the sync state and
the cleanup of deleted records. Give every company its own name, even in one dataset — two
companies sharing a name would forget each other's records, so a run refuses state that was
written for another company. Pass `check_active` to let a host stop a sync whose access was
revoked; it is called before every request.

## What gets ingested

One document per record, with ids turned into names (owners, pipelines, stages, labels, custom
fields and their options):

- **Deals**: title, status (and whether it is archived), pipeline, stage, value, organization,
  person, owner, expected close date, won or lost date, lost reason, labels, custom fields.
- **Persons**: name, job title, organization, owner, labels, custom fields.
- **Organizations**: name, owner, address, labels, custom fields.
- **Activities**: subject, type, due date, done, the deal, person and organization it belongs
  to, owner, location, public description and its note.
- **Notes**: the note text (Pipedrive stores notes as HTML; it is turned into plain text), the
  deal, person and organization it is attached to, author and date.

Notes are their own documents, so editing one note re-processes only that note. Each note
and activity names the records it belongs to, and the graph links them through those names.
Email addresses and phone numbers of persons are left out on purpose.

## How sync + forget-on-delete work

Pipedrive has a delete feed only for deals (and only for 30 days), and none for persons,
organizations, activities or notes. So each run lists the selected records and emits only
those whose rendered text changed, using a fingerprint kept in dlt's per-resource state.

`update_time` alone is not used as the cursor because it misses changes that matter here:
renaming a deal does not bump the `update_time` of its notes, but every note document names
its deal, so the note must be re-rendered. Comparing the rendered text catches that, and also
custom field or stage renames, while records that did not change are never re-processed.

A record that stops showing up is checked once more by id before it is forgotten: a deleted
record (404, or flagged `is_deleted`), an archived deal when archived deals are left out, or a
record of a kind dropped from `include` is emitted with the `_deleted` hard-delete marker, dlt
drops it on `merge`, and cognee's `orphan_cleanup` removes it from the graph + vector +
relational stores. A record that is still there is kept. Keep `resource_name` and the dataset
fixed across runs.

A run never sleeps for long on Pipedrive's rate limits. A short burst limit (`429` with a
`Retry-After` of a few seconds) is waited out; a spent daily budget, or `max_requests`
(1000 by default), stops the run cleanly. It keeps its progress, reports it in
`source.cognee_sync_stats`, and the next run resumes on the page it stopped at. Deletions are
only decided once a full cycle over every listing has finished.
A record whose check fails is kept as it is, counted in `failed` and retried on the next run,
and the rest of the sync goes on.

## Limitations

- Every cycle lists all selected records, which costs Pipedrive API tokens: about 10 per page
  of 500 for the v2 listings and 20 per page of 500 notes, plus one or two per re-checked
  record. Listing 10,000 deals and 20,000 notes costs roughly 1,000 tokens per run, well
  inside the daily budget of even one seat, but a sync every few minutes would add up.
- Leads, products, projects, files and email threads are not synced.
- Only records the token's user can see are synced. When a record becomes hidden from that
  user, the re-check decides: a 404 forgets it, a 403 keeps it and counts it in `failed`.
- The dataset holds deal values, people's names and note text. With cognee's access control
  (on by default) only the user who ran the sync, and users granted permission on the dataset,
  can read it; keep it in its own dataset and share it only with people allowed to see the CRM.

## Setup

1. In Pipedrive open **Settings → Personal preferences → API** and copy your personal API
   token. The token has the same access as your user, so use a user that can see the records
   you want in memory.
2. Pass it as `api_token` (the connector only lists and reads records), plus your
   `LLM_API_KEY` like any other cognee run.

## Testing

```bash
uv run --with pytest --with pytest-asyncio pytest tests/
```

The tests fake the Pipedrive API (no live token) and include offline end-to-end runs through a
real `dlt` merge and through `cognee.add` and `cognify`, proving a deleted record is removed
from the graph, not only from staging.
