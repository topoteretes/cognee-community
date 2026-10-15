# cognee-community-connector-nuclino

A Nuclino data-source connector for [cognee](https://github.com/topoteretes/cognee): sync your Nuclino workspace into memory — "ask my Nuclino".

This connector synchronizes Nuclino **items**, **collections**, and their **Markdown content** into Cognee as **document-mode sources**. Ingested pages flow through Cognee's cognify entity-extraction pipeline (rather than legacy relational DLT tables) while supporting incremental updates and forget-on-delete.

## Features

- **API-Key Authentication**: Connect securely via Nuclino's REST API using `NUCLINO_API_KEY` or the `api_key` parameter.
- **Flexible Ingestion Scope**: Sync all accessible workspaces, or target specific workspace IDs.
- **Team-Filtered Discovery**: Scope workspace discovery to a specific team with `team_id`.
- **Items & Collections**: Ingests both hierarchical collections and individual items.
- **Markdown Ingestion**: Preserves formatting, titles, and item URLs for rich semantic recall.
- **Incremental Synchronization**: Tracks per-object `lastUpdatedAt` timestamps in DLT resource state.
- **Efficient Network Use**: Lightweight metadata sweep skips unchanged items; only new and modified objects fetch full content.
- **Upstream Deletion Propagation**: Removed items emit hard-delete tombstones that trigger Cognee orphan cleanup.
- **Fail-Closed Synchronization**: Aborts on API or network errors to preserve existing state and prevent false orphan deletion.
- **Cognee Document-Mode Ingestion**: Automatically tags resources with `DOCUMENT_SOURCE_ATTR = "nuclino"` for document pipeline routing.

## Installation

Install the published package:

```bash
uv pip install cognee-community-connector-nuclino
```

Or install from the local repository for development:

```bash
cd packages/connector/nuclino
uv sync
```

> **Note**: This is a standalone community package (`cognee-community-connector-nuclino`). Do not use `pip install "cognee[nuclino]"`.

## Authentication

Set your Nuclino API key as an environment variable:

```bash
export NUCLINO_API_KEY="your-nuclino-api-key"
```

You can obtain an API key from your Nuclino account under **Team Settings > Integrations > API Keys**.

> **Note**: The Nuclino REST API expects the raw key directly in the `Authorization: <API_KEY>` header (unlike Bearer tokens). The connector formats this header internally; you only need to provide the raw key string via environment variable or the `api_key` argument.

## Basic Usage

```python
import asyncio
import cognee
from cognee_community_connector_nuclino import nuclino_source


async def main():
    # Sync a specific workspace into Cognee
    source = nuclino_source(
        workspace_ids=["<workspace-id>"],
    )

    await cognee.remember(
        source,
        dataset_name="nuclino",
        primary_key="id",
        write_disposition="merge",
    )

    # Query your Nuclino knowledge base
    answer = await cognee.recall("What information is stored in my Nuclino workspace?")
    print("Answer:", answer)


if __name__ == "__main__":
    asyncio.run(main())
```

> **IMPORTANT**: `write_disposition="merge"` is required for incremental synchronization and deletion reconciliation. Cognee's `resolve_dlt_sources()` defaults caller-level `write_disposition` to `"replace"`. Passing `"merge"` ensures DLT performs incremental upserts and properly processes `_deleted` tombstones so Cognee's orphan cleanup can remove deleted documents.

## Scope Selection

### Sync All Accessible Workspaces

Omit `workspace_ids` to automatically discover and ingest every workspace accessible to your API key:

```python
source = nuclino_source()
```

### Select Specific Workspaces

Provide an explicit list of workspace IDs to restrict synchronization:

```python
source = nuclino_source(
    workspace_ids=["workspace-1", "workspace-2"],
)
```

### Team-Filtered Discovery

Filter discovered workspaces by team:

```python
source = nuclino_source(team_id="<team-id>")
```

> **Scope Stability Invariant**: The connector's synchronization state (`item_versions`) tracks the entire selected corpus for the given dataset. If you reduce or change `workspace_ids` between runs on the *same* dataset, objects from workspaces that are no longer targeted will be recognized as missing from the corpus and deleted via Cognee's orphan cleanup. Always maintain a stable ingestion scope for a dataset, or use separate datasets (e.g. `dataset_name="nuclino_workspace_1"`) when targeting different scopes.

## Querying Cognee

Once ingested with `remember()`, your Nuclino documents are part of Cognee's knowledge graph and vector indexes. You can query them using `cognee.recall()` or `cognee.search()`:

```python
# Semantic recall
answer = await cognee.recall("Summarize our team's engineering onboarding guide.")
print(answer)

# Graph completion search
results = await cognee.search(
    query_text="What are our project deadlines?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["nuclino"],
)
print(results)
```

## How Incremental Synchronization Works

The connector persists per-object version timestamps (`lastUpdatedAt`) in DLT's resource state:

- **Run 1 (Initial Sync)**:
  1. Lists all items and collections across target workspaces.
  2. Fetches full Markdown content for each object.
  3. Records per-object `lastUpdatedAt` timestamps in resource state.
  4. Cognee ingests records as document-mode sources.

- **Run 2+ (Incremental Re-sync)**:
  1. Performs a lightweight metadata sweep (`GET /v0/items`) across target workspaces.
  2. Compares each object's `lastUpdatedAt` with the stored version in resource state.
  3. **Unchanged objects** are skipped without fetching full content.
  4. **New or modified objects** fetch full content and update the version map.
  5. **Missing objects** trigger deletion propagation.

### Deletion Behavior

When an item or collection is deleted or unshared in Nuclino:

1. The metadata sweep detects that a previously known ID is no longer returned by the API.
2. The connector yields a tombstone record with `_deleted: True`.
3. DLT's `merge` disposition applies the hard delete to the staging table.
4. Cognee's fresh-ID reconciliation identifies the dropped document.
5. Cognee's `orphan_cleanup` purges the document from vector, graph, and relational stores.

### Empty-Corpus Deletion & Failure Safety

- **Authoritative Empty Corpus**: If all items and collections in the selected workspace(s) are deleted upstream, a successful metadata sweep returning zero objects will emit hard-delete tombstones for all previously tracked objects and reset the persisted sync state to empty, removing them from Cognee via orphan cleanup.
- **Fail-Closed Safety**: Any transient HTTP errors, network timeouts, malformed API payloads, or mid-sync failures abort execution immediately. In failure cases, the previously persisted sync state remains completely untouched and no false deletion tombstones are emitted.

## Testing

Run unit and integration tests (mocked HTTP API, no live credentials or LLM keys required):

```bash
uv run --with pytest --with pytest-asyncio pytest tests/ -v
```

Or using an existing environment with dependencies installed:

```bash
pytest tests/ -v
```
