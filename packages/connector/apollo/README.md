# cognee-community-connector-apollo

An Apollo.io data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your team's contacts, accounts and sequence activity into memory — "ask my CRM".

It exposes a `dlt` resource you hand to `cognee.remember(...)`, reusing cognee's existing DLT
ingestion path (`resolve_dlt_sources` → `ingest_dlt_source` → `orphan_cleanup`) — so you get
**incremental re-sync** (only records whose content changed are re-processed) and
**forget-on-delete** (records deleted in Apollo are purged from memory on the next sync) with no
core changes.

## Requirements

- cognee 1.6.1 or later (pinned to `1.6.3`), for table-scoped document cleanup.
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
    apollo_source(api_key="..."),
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
`resource_name` (default `"apollo"`) and the dataset fixed across runs.

A run never sleeps on Apollo's rate limits. When the limit or `max_requests` is reached it stops
cleanly, keeps its progress and reports it in `source.cognee_sync_stats`; the next run resumes.
Apollo search returns at most 50,000 records, so a larger workspace must be narrowed with stage
or list filters, otherwise deletions are not detected.

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
