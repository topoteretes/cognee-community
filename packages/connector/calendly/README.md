# cognee-community-connector-calendly

A Calendly data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your scheduled events into memory — "ask my calendar".

It exposes a `dlt` resource you hand to `cognee.remember(...)` / `cognee.add(...)`.
Each scheduled event becomes a **document** whose content includes the event name and
timing, the meeting notes, and — most importantly — every invitee's
**questions & answers** (the custom-question responses people gave when they booked).
Events flow through cognee's cognify entity-extraction pipeline (not the relational
dlt-row path) via cognee's document-mode marker.

## Requirements

> **This connector requires a cognee release that ships "document-mode"** — i.e.
> `cognee.tasks.ingestion.dlt_utils.DOCUMENT_SOURCE_ATTR` and the `resolve_dlt_sources`
> routing that reads it (first shipped in cognee 1.4.0).

## Install

```bash
uv pip install cognee-community-connector-calendly
# or, from this monorepo:
cd packages/connector/calendly && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_calendly import calendly_source

await cognee.remember(
    calendly_source(),  # CALENDLY_API_TOKEN from env, or pass token=...
    dataset_name="calendly",
    write_disposition="merge",  # REQUIRED — incremental sync
    max_rows_per_table=0,       # see note below
)

answer = await cognee.search(
    query_text="What did people ask for in their booking questions?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["calendly"],
)
```

Scope what you ingest with `invitee_email="someone@example.com"` to sync only events
with that invitee; omit it to sync every event your token can see.

## How sync + forget-on-delete work

The source is **incremental**, keyed on each event's `updated_at` (last-modified)
timestamp — the same pattern the Confluence connector uses:

- **First run** does a full backfill of active events and records the set of event ids
  (`known_ids`) plus the highest `updated_at` (`last_updated`) in dlt's per-resource
  state.
- **Later runs** do one cheap metadata sweep of the current *active* events and
  re-fetch only those whose `updated_at` advanced since `last_updated` (plus anything
  new to the corpus). Because the cursor is `updated_at`, not `start_time`, a meeting
  note added **after an event ends** is picked up on the next sync.
- **Forget-on-delete** — Calendly has no delete feed, so each run compares the ids it
  saw last run against the current active sweep. A canceled (or vanished) event drops
  out of the active listing and is emitted with the `_deleted` hard-delete marker;
  `dlt` removes it on `merge`, and cognee's existing `orphan_cleanup` purges it from
  the graph + vector + relational stores.

Unchanged events are skipped and keep a stable content-hash `data_id`, so they are not
re-cognified (no LLM cost) — only their invitee details are not re-fetched.

### Limitations

- The cursor is `updated_at`, so a change is only picked up if Calendly bumps it.
  Canceling/rescheduling an event, editing its notes, or changing invitee answers all
  bump `updated_at`. A change Calendly records without bumping `updated_at` would be
  missed until the event's next change.
- Keep the same scope (`invitee_email`, or omit it) across runs: the id set and cursor
  are stored per dataset, and changing scope between runs triggers a full reconcile.
- Pass `max_rows_per_table=0` in `remember(...)`: cognee's default row cap (50) would
  make orphan-cleanup compare against a truncated window and could wrongly forget rows.

## Setup

1. Create a Personal Access Token at https://calendly.com/integrations/api_webhooks.
2. Set it as `CALENDLY_API_TOKEN` (or pass `token=...`), plus your `LLM_API_KEY` like
   any other cognee run.

## Testing

```bash
uv run pytest tests/
```

The tests mock the Calendly API (no live token) and cover event rendering, pagination,
the incremental `updated_at` cursor, and forget-on-delete (cancel / vanish on re-sync).
