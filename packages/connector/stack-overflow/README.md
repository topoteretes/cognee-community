# cognee-community-connector-stack-overflow

A Stack Overflow data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync tagged questions (and their accepted/top answers) into memory —
"ask my tag's Q&A".

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`. Each
question is rendered to plain text and ingested as a **normal document** (it flows
through cognee's cognify entity-extraction pipeline, not the deterministic dlt-row
path), via cognee's document-mode marker.

## Install

```bash
uv pip install cognee-community-connector-stack-overflow
# or, from this monorepo:
cd packages/connector/stack-overflow && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_stack_overflow import stack_overflow_source

await cognee.remember(
    stack_overflow_source(tags=["python", "asyncio"]),
    dataset_name="stack_overflow",
)

answer = await cognee.search(
    query_text="How do people handle cancellation in asyncio?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["stack_overflow"],
)
```

`tags` is **required** (or set `STACK_OVERFLOW_TAGS`, comma-separated) — Stack
Overflow's daily quota is small and "sync all of Stack Overflow" is not a meaningful
target. See `examples/example.py` for the full flow.

## How sync + forget-on-delete work

Each run does a cheap listing sweep of every question currently matching `tags`
(ids + `last_activity_date` only, no bodies) — that sweep drives deletion detection,
while questions newer than the stored cursor have their body and answers fetched and
emitted with `write_disposition="merge"` (idempotent upsert by question id). Stack
Exchange has no delete feed, so a question absent from the sweep (deleted, or moved
out of tag scope) is emitted with an `_deleted` hard-delete marker; dlt drops it on
merge and cognee's existing `orphan_cleanup` removes it from the graph and vector
stores.

## Setup

1. (Optional but recommended) Register a Stack Apps application at
   <https://stackapps.com/apps/oauth/register> to get an API key — keyless access is
   capped at 300 requests/day, a key raises that to 10,000/day.
2. Set the key as `STACK_OVERFLOW_API_KEY` (or pass `api_key=...`), plus your
   `LLM_API_KEY` like any other cognee run.

## Testing

```bash
uv run pytest tests/
```

The tests mock the Stack Exchange API (no live key) and cover HTML rendering, answer
selection, pagination, and incremental sync + forget-on-delete (edit / delete on
re-sync). They require a cognee build that includes document-mode (see the pinned
`cognee` version in `pyproject.toml`).
