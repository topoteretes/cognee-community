# cognee-community-connector-stack-overflow

A Stack Overflow data-source connector for [cognee](https://github.com/topoteretes/cognee):
ingest questions, answers, and accepted solutions by tag — "ask my Stack Overflow".

## Requirements

> **Requires cognee 1.4.0+** (ships `DOCUMENT_SOURCE_ATTR` + `resolve_dlt_sources` document-mode).

## Install

```bash
# From the monorepo:
cd packages/connector/stack-overflow && uv sync --all-extras

# Or from PyPI (once published):
pip install cognee-community-connector-stack-overflow
```

## Setup

```bash
export STACKOVERFLOW_API_KEY=your_key_here   # optional; 300 req/day without, 10 000 with
```

Get a free key at https://stackapps.com/apps/oauth/register

## Usage

```python
import cognee
from cognee_community_connector_stack_overflow import (
    plan_stack_overflow_sync,
    stack_overflow_source,
)

# 1. Estimate request cost before ingesting (no data is fetched)
print(plan_stack_overflow_sync(tags=["polars", "duckdb"]))

# 2. Ingest
await cognee.remember(
    stack_overflow_source(
        tags=["polars", "duckdb"],
        max_questions_per_tag=500,
        min_score=5,
        min_answers=1,
    ),
    dataset_name="stackoverflow",
    primary_key="id",
    write_disposition="merge",
)

# 3. Search
results = await cognee.search(
    "How do I lazily scan a CSV in polars?",
    datasets=["stackoverflow"],
)
```

## Incremental Sync

Re-running `cognee.remember(...)` with the same `dataset_name` picks up only
questions changed since the last run (new answers, edits). The cursor is
persisted in dlt resource state.

## Forget-on-Delete

On each sync, previously ingested question IDs are rechecked via the API.
Questions deleted upstream are tombstoned and removed from the cognee graph
on the next sync.

## Options

| Parameter | Default | Description |
|---|---|---|
| `tags` | `["python"]` | Tags to ingest (one stream per tag) |
| `max_questions_per_tag` | `500` | Hard cap per tag per run |
| `min_score` | `0` | Skip questions below this score |
| `min_answers` | `0` | Skip questions with fewer answers |
| `max_requests_per_run` | `None` | Budget cap (recommended for keyless use) |
| `max_delete_ratio` | `0.2` | Safety guard — abort if > 20% would be deleted |
| `preview_deletions` | `False` | Log deletions without applying them |

## Running Tests

```bash
cd packages/connector/stack-overflow
uv run pytest tests/ -v
```