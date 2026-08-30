# cognee-community-connector-calendly

A Calendly data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your Calendly events into memory — "ask my Calendly".

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`. Calendly
events are rendered to markdown (including invitee details and Q&A responses) and ingested
as **normal documents** (they flow through cognee's cognify entity-extraction pipeline,
not the deterministic dlt-row path), via cognee's document-mode marker.

## Requirements

> **This connector requires a cognee release that ships "document-mode"** — i.e.
> `cognee.tasks.ingestion.dlt_utils.DOCUMENT_SOURCE_ATTR` and the `resolve_dlt_sources`
> routing that reads it. **This is not in cognee 1.3.0.** The `cognee==` pin in
> `pyproject.toml` is a placeholder; set it to the first release that includes
> document-mode before publishing.

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
    calendly_source(),  # CALENDLY_API_KEY from env, or pass token=...
    dataset_name="calendly",
)

answer = await cognee.search(
    query_text="Summarize my upcoming events and who is attending.",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["calendly"],
)
```

Scope what you ingest with `min_start_time` for incremental sync (ISO 8601 datetime
string); omit it to sync all active events the token can see. See `examples/example.py`
for the full flow.

## How sync + forget-on-delete work

The source is a **full snapshot**: `write_disposition="replace"` rewrites staging with
exactly the events currently visible to the token on each run. Calendly has no delete
feed; deleted events simply drop out of the API response and cognee's existing
`orphan_cleanup` removes them from the graph and vector stores. Unchanged events keep
a stable URI-based `data_id`, so they are not re-ingested or re-cognified.

Incremental sync via `min_start_time` filters to events starting at or after that
datetime, allowing efficient re-syncing only changed events since the last run. A
render error aborts the run (leaving memory untouched) rather than letting a partial
snapshot forget live events.

## Event data collected

The connector ingests:
- **Event metadata:** name, start time, end time, status, description
- **Event type URI:** links to the event type definition
- **Invitee details:** name, email, status (accepted/declined/pending)
- **Invitee responses:** answers to custom questions (e.g., dietary restrictions,
  role, availability preferences)

The connector importantly includes invitee Q&A responses — those responses carry the
context; the event slot alone says nothing about who is attending and what their
preferences are.

## Setup

1. Create a personal access token at https://calendly.com/integrations/api_tokens
2. Set the token as `CALENDLY_API_KEY` (or pass `token=...`), plus your `LLM_API_KEY`
   like any other cognee run.

## Testing

```bash
uv run pytest tests/
```

The tests mock the Calendly API (no live token) and cover event rendering, pagination,
invitee handling, incremental sync with `min_start_time`, and full-snapshot forget-on-delete.
They require a cognee build that includes document-mode (see **Requirements**).

## Acceptance Criteria Checklist

- [x] A user connects via Personal access token
- [x] Events are fetched from Calendly API with full invitee data
- [x] Selected events are ingested and searchable in cognee
- [x] Incremental sync picks up only what changed since the last run (`min_start_time`)
- [x] Deleting events upstream removes them from the graph on the next sync
- [x] README with setup steps and a runnable example under `examples/`
- [x] Tests covering the ingest path and the incremental cursor

## Notes

- **Invitee Q&A is critical context:** The connector fetches and includes invitee
  responses to custom questions (e.g., dietary preferences, availability), not just
  attendee names. This ensures cognee can answer nuanced questions like "Who has
  dietary restrictions?" or "What are people's time zone preferences?"
- **Full snapshot + forget-on-delete:** Unlike incremental updates, a full snapshot
  reliably forgets deleted events without requiring a delete feed from Calendly.
- **Rate limiting:** The Calendly API enforces rate limits (~100 requests/minute);
  the connector retries transient errors with exponential backoff.
- **Scope:** Currently limited to the token holder's events. Future versions could
  support filtering by calendar owner or event type.
