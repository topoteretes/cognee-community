# cognee-community-connector-apollo

An Apollo.io data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your team's contacts, accounts and sequence activity into memory — "ask my CRM".

It exposes a `dlt` resource you hand to `cognee.remember(...)`, reusing cognee's existing DLT
ingestion path (`resolve_dlt_sources` → `ingest_dlt_source` → `orphan_cleanup`) — so you get
**incremental re-sync** (only records whose content changed are re-processed) and
**forget-on-delete** (records deleted in Apollo are purged from memory on the next sync) with no
core changes.

## Requirements

- Python 3.10 to 3.14.
- cognee 1.6.3 or later (below 2), for table-scoped document cleanup.
- An Apollo plan with API access to contacts and accounts search. Free plans don't have it; any
  paid plan or a trial does.

## Install

```bash
uv pip install cognee-community-connector-apollo
# or, from this monorepo:
cd packages/connector/apollo && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_apollo import apollo_source

await cognee.remember(
    apollo_source(api_key="...", resource_name="apollo_sales"),
    dataset_name="apollo_crm",
    primary_key="id",
    write_disposition="merge",  # incremental upsert by record id
    max_rows_per_table=0,  # unlimited: orphan-cleanup sees the whole corpus
)

answer = await cognee.search(
    query_text="Which contacts at Acme are in an active sequence?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["apollo_crm"],
)
```

Choose what to sync with `include=("contacts", "accounts", "sequences")`, and narrow it with
`contact_stage_ids`, `contact_label_ids`, `account_stage_ids` and `account_label_ids` (labels are
Apollo's lists). Re-running `remember(...)` with the same dataset syncs only records changed
since the last run and forgets records that were deleted. See `examples/example.py` for the full
flow.

> **`write_disposition="merge"` is required** — the add pipeline defaults to `"replace"`,
> which would wipe the synced records on the second sync.

`resource_name` is required and names one Apollo account's sync: it keys the sync state and
the cleanup of deleted records. Give every Apollo account its own name, even in one dataset —
two accounts sharing a name would forget each other's records, so a run refuses state that was
written for another Apollo team. Pass `check_active` to let a host stop a sync whose access was
revoked; it is called before every request.

## What gets ingested

Only your own CRM records. Apollo's enrichment database (people and organization search,
firmographics, intent signals) is never read, and every endpoint used costs 0 Apollo credits.

- **Contacts**: name, job title, company, stage, lists, custom fields, sequence memberships and
  recent sequence activity (enrolled, paused, replied, ...).
- **Accounts**: name, domain, stage, lists, custom fields.
- **Sequences**: name, status and number of steps.

Emails, phone numbers and social profiles are left out on purpose.

## How sync + forget-on-delete work

Apollo has no change feed and no deleted flag, so each run lists the selected records and emits
only those whose rendered text changed, using a fingerprint kept in dlt's per-resource state.
Contact `updated_at` is not used: sequence activity does not bump it, and accounts do not return
one.

A record that stops showing up is checked once more before it is forgotten: a deleted record, or
one that left the selected stages or lists, is emitted with the `_deleted` hard-delete marker,
dlt drops it on `merge`, and cognee's `orphan_cleanup` removes it from the graph + vector +
relational stores. Deselecting a kind in `include` forgets its records too. Keep
`resource_name` and the dataset fixed across runs.

A run never sleeps on Apollo's rate limits. When the limit or `max_requests` is reached it stops
cleanly, keeps its progress and reports it in `source.cognee_sync_stats`; the next run resumes.
A record whose check or activity read fails is kept as it is, counted in `failed` and retried
on the next run, and the rest of the sync goes on.

## Limitations

- Apollo search returns at most 50,000 records, so a larger workspace must be narrowed with
  stage or list filters, otherwise deletions are not detected.
- The activity feed is not paged: a contact keeps at most 50 events, and a cut feed is marked
  `Sequence activity (cut at 50 events)` in the document.
- Apollo limits requests per minute as well as per hour and day (50 a minute on the free plan,
  200 on paid plans). A run stops once fewer than 10 are left in any window, so on a small plan
  the minute window usually ends a run first and a large workspace takes several runs.
- The dataset holds contact names, job titles and companies. With cognee's access control (on
  by default) only the user who ran the sync, and users granted permission on the dataset, can
  read it; keep it in its own dataset and share it only with people allowed to see the CRM.

## Setup

1. Create an API key in Apollo under **Settings → Integrations → API**. A scoped key needs:
   `api/v1/contacts/search`, `api/v1/contacts/show`, `api/v1/accounts/search`,
   `api/v1/accounts/show`, `api/v1/emailer_campaigns/search`,
   `api/v1/emailer_campaigns/activity_feed`, `api/v1/contact_stages/index`,
   `api/v1/account_stages/index`, `api/v1/labels/index` and `api/v1/fields/index`.
2. Pass it as `api_key` (read-only: the connector only lists and reads records), plus your
   `LLM_API_KEY` like any other cognee run.

## Testing

```bash
uv run --with pytest --with pytest-asyncio pytest tests/
```

The tests fake the Apollo API (no live key) and include offline end-to-end runs through a real
`dlt` merge and through `cognee.add` and `cognify`, proving a deleted record is removed from the
graph, not only from staging.
