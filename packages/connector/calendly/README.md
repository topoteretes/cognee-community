# cognee-community-connector-calendly

A Calendly data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your scheduled meetings into memory — "ask my calendar".

Implements [topoteretes/cognee#4816](https://github.com/topoteretes/cognee/issues/4816).

It exposes a `dlt` resource you hand to `cognee.remember(...)` / `cognee.add(...)`. Each
Calendly **scheduled event** is ingested as one document together with every **invitee**'s
status, their **questions/answers**, and free-text **notes** — an event slot on its own is
close to meaningless, so folding the invitee's answers into the same document lets cognee's
entity extraction link them together, rather than ingesting a bare, unlinked time slot.

## Requirements

> **This connector requires a cognee release that ships "document-mode"** — i.e.
> `cognee.tasks.ingestion.dlt_utils.DOCUMENT_SOURCE_ATTR` and the `resolve_dlt_sources`
> routing that reads it (same requirement as the Notion and Google Drive connectors).
> The `cognee==` pin in `pyproject.toml` is a placeholder; set it to the first release
> that includes document-mode before publishing.

## Install

```bash
uv pip install cognee-community-connector-calendly
# or, from this monorepo:
cd packages/connector/calendly && uv sync --all-extras
```

## Setup

1. Create a Personal Access Token at
   https://calendly.com/integrations/api_webhooks (or **Integrations & apps →
   API and webhooks** in your Calendly settings).
2. Set it as `CALENDLY_API_TOKEN` (or pass `token=...`), plus your `LLM_API_KEY` like
   any other cognee run.

## Usage

```python
import cognee
from cognee_community_connector_calendly import calendly_source

await cognee.remember(
    calendly_source(),          # CALENDLY_API_TOKEN from env, or pass token=...
    dataset_name="calendly",
    primary_key="id",
    write_disposition="merge",  # REQUIRED — see "How sync works" below
    max_rows_per_table=0,       # REQUIRED for a real calendar — see note below
)

answer = await cognee.search(
    query_text="What did people say they wanted to discuss in our recent meetings?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["calendly"],
)
```

By default the connector syncs the token owner's own events. Pass `organization_uri=...`
to sync every event across an organization (requires an admin/owner token), or
`user_uri=...` to sync a specific user's events. See `examples/example.py` for the full
flow, including a second, incremental sync.

> **Important:** `write_disposition="merge"` is required. The `add`/`remember` pipeline
> defaults to `"replace"` (drop + reload each run), which would wipe the whole synced
> calendar on the second sync. `max_rows_per_table=0` disables cognee's per-table read cap
> (default 50) so forget-on-delete compares against the *entire* synced calendar, not a
> truncated window.

## How sync + forget-on-delete work

**Auth** is a Calendly Personal Access Token, sent as a bearer token; the connector only
issues `GET` requests (`/users/me`, `/scheduled_events`, `/scheduled_events/{uuid}/invitees`).

**Incremental sync** uses a `min_start_time` window as its cursor, persisted in dlt's
per-resource state:

- The first sync backfills events starting from `now - lookback_days` (default 7 days)
  onward — there is no upper bound, so every future event is always included.
- Every later sync narrows the floor to `now - recheck_window_days` (default 2 days), so
  each run re-checks only a rolling recent window for changes/cancellations, plus (again)
  every future event.

**Forget-on-delete**: Calendly does not hard-delete scheduled events — the equivalent
signal is `status="canceled"`. A canceled event is emitted with the `_deleted` hard-delete
marker; dlt removes that row from its destination on `merge`, and cognee's existing
`orphan_cleanup` then purges it from the graph, vector, and relational stores. An invitee
who cancels on an otherwise-active event is reflected the next time that event is
re-synced (their status/answers are re-rendered into the same event document).

**Limitation:** a change to an event more than `recheck_window_days` in the past (e.g. a
very late cancellation) is not picked up by a later sync, since it falls outside the
`min_start_time` floor. Pass a larger `recheck_window_days` if your workflow needs a
longer memory of after-the-fact changes.

## Testing

```bash
uv run pytest tests/
```

The tests mock the Calendly REST API (no live token, no network) and cover: pagination,
the `min_start_time` cursor advancing across backfill → incremental runs, event+invitee
rendering (including notes/Q&A), canceled events emitting hard-delete markers, the dlt
resource's merge + hard-delete wiring, and (end-to-end, via a real dlt pipeline into a
temp sqlite/duckdb destination) that a canceled event is actually removed from the
destination on the next sync. Tests requiring `dlt`/`duckdb`/`cognee` are skipped
automatically (`pytest.importorskip`) if those are not installed.
