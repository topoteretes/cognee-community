# cognee-community-connector-splitwise

A Splitwise data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync shared expenses, groups, and balances into memory — "ask who owes what".

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`.
Splitwise expenses are ingested as **normal documents** (they flow through cognee's
cognify entity-extraction pipeline, not the deterministic dlt-row path) via cognee's
`DOCUMENT_SOURCE_ATTR` document-mode marker.

## Features

- **Expenses, Groups, Balances**: Ingests expense descriptions, costs, splits,
  group membership, and net balances into unified documents with structured columns.
- **Personal Token Auth**: Uses a Splitwise personal access token (`SPLITWISE_API_KEY`).
- **Incremental Sync**: `updated_at` high-watermark in dlt `resource_state` skips
  unchanged expenses on re-sync.
- **Content-Hash Watermark**: SHA-256 over description + cost + shares avoids
  LLM re-processing when only timestamps change.
- **Forget-on-Delete (Merge + Tombstone)**: Voided expenses (`deleted_at` set)
  emit `_deleted=True` hard-delete markers, purging them from the graph on merge.
- **Group Filtering**: Optional server-side `group_id` filter to scope ingestion.
- **PII-Safe**: Emails never appear in document prose; only hashed user IDs in
  structured `shares_json` column.

## Install

```bash
uv pip install cognee-community-connector-splitwise
# or, from this monorepo:
cd packages/connector/splitwise && uv sync
```

## Setup

1. Obtain a Splitwise personal access token from
   [https://secure.splitwise.com/oauth/tokens](https://secure.splitwise.com/oauth/tokens).
2. Set `SPLITWISE_API_KEY` (or pass `api_key=...`), plus your LLM API key:

```bash
export SPLITWISE_API_KEY="your_personal_token"
export LLM_API_KEY="sk-..."
```

## Usage

```python
import cognee
from cognee_community_connector_splitwise import splitwise_source

# Ingest all expenses (or filter to specific groups)
await cognee.remember(
    splitwise_source(
        group_ids=[12345],  # optional group filter
        include_groups=True,
        include_balances=True,
        max_expenses=1000,
    ),
    dataset_name="splitwise",
)

# Query knowledge graph
answer = await cognee.search(
    query_text="Who still owes me over 50 USD across all groups?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["splitwise"],
)
```

See `examples/basic_usage.py` for a full runnable example.

## Testing

```bash
cd packages/connector/splitwise
uv run --with pytest pytest tests/
```

All tests are fully mocked (no live API key or network access required).

## License

MIT