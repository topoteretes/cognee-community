# cognee-community-connector-canny

A Canny data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync feature requests, comments and **vote counts** into memory - "what do my users want most?".

It exposes a `dlt` resource you hand to `cognee.remember(...)`. Each post is ingested as a
**normal document** (cognee's document-mode), so it flows through cognify entity extraction.

## Requirements

cognee **>= 1.4.0** (document-mode: `DOCUMENT_SOURCE_ATTR`).

## Install

```bash
uv pip install cognee-community-connector-canny
# or, from this monorepo:
cd packages/connector/canny && uv sync --all-extras
```

## Setup

1. Copy your **secret API key** from Canny company settings -> API.
2. Export it as `CANNY_API_KEY` (or pass `api_key=...`), plus `LLM_API_KEY`.

## Usage

```python
import cognee
from cognee_community_connector_canny import canny_source

await cognee.remember(canny_source(), dataset_name="canny")

answer = await cognee.search(
    query_text="Which feature requests have the most votes?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["canny"],
)
```

| Argument | Meaning |
| --- | --- |
| `api_key` | Secret API key. Defaults to `CANNY_API_KEY`. |
| `board_ids` | Only these boards. Default: all. |
| `statuses` | Only these statuses, e.g. `["planned", "in progress"]`. |
| `include_comments` | Include each post's comments (default `True`). |
| `include_internal` | Also include internal admin-only comments (default `False`). |

See `examples/example.py` for a runnable script.

## What a document contains

Title, details, board, category, status, **votes (`score`)**, comment count, author, tags,
creation time and the comments (oldest first). Votes are in the text so the graph can weigh demand.

## How sync + forget-on-delete work

Canny's `posts/list` has no `updatedAfter` filter and there is no delete feed. So the source is
a **full snapshot** (`write_disposition="replace"`, cognee's default), like the Notion and Slack
connectors:

* Each run rewrites staging with exactly the posts currently visible.
* A post deleted in Canny (or merged away, or out of scope) drops out, and cognee's
  `orphan_cleanup` removes it from the graph and vector stores.
* Unchanged posts have the same id and content, so they keep the same content-hash `data_id`
  and are **not re-ingested or re-cognified**. Only new or changed posts (new comment, vote or
  status change, edited text) are processed again.
* Any error aborts the run, so a partial read never causes false deletions.
* `429`/`5xx`/network errors are retried with backoff (honors `Retry-After`). A bad key raises
  `PermissionError`.

Do **not** pass `write_disposition="merge"` or `"append"` - they would break forget-on-delete.

### Cost

Posts are read 100 per request. Each post with comments needs one extra request for them. The
free plan allows 100 requests/minute, so large boards take a while.

## Testing

```bash
uv run pytest tests/
```

Tests use a fake HTTP client and a temporary SQLite destination (no key, no network).
