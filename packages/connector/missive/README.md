# cognee-community-connector-missive

A Missive data-source connector for [cognee](https://github.com/topoteretes/cognee): sync your Missive conversations, messages, and comments into AI memory.

It exposes a `dlt` source passed to `cognee.remember(...)` / `cognee.add(...)`. Conversations are formatted into structured markdown documents and ingested as normal documents through Cognee's cognify entity-extraction pipeline.

## Install

```bash
pip install cognee-community-connector-missive
```

## Usage

```python
import cognee
from cognee_community_connector_missive import missive_source

source = missive_source()
await cognee.remember(source, dataset_name="missive")

answer = await cognee.search(
    query_text="Summarize recent customer support issues.",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["missive"],
)
```

## Configuration

Set the environment variable or pass arguments directly:

```bash
export MISSIVE_API_TOKEN="your_missive_api_token"
```

Parameters supported by `missive_source`:

- `api_token`: Missive API Bearer token (defaults to `MISSIVE_API_TOKEN` environment variable).
- `mailbox`: Restrict ingestion to a specific mailbox ID.
- `team`: Restrict ingestion to a specific team ID.
- `label`: Restrict ingestion to a specific label ID.
- `since`: Only ingest conversations modified after this Unix timestamp or ISO string.
- `include_comments`: Whether to include internal team comments (default: `True`).
- `include_contact_details`: Whether to include email addresses and phone numbers (default: `False`).

## Sync and Forget-on-Delete

The connector operates as a snapshot sync with `write_disposition="replace"`. Deleted or trashed conversations upstream are omitted from subsequent syncs, allowing Cognee's orphan cleanup pipeline to remove obsolete nodes from the knowledge graph and vector database.
