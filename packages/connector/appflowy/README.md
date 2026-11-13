# AppFlowy Connector for Cognee

A production-grade [Cognee](https://github.com/topoteretes/cognee) community data-source connector for **[AppFlowy](https://appflowy.io/)**, the leading open-source alternative to Notion (with over 55,000 GitHub stars) designed for privacy-first team wikis, project databases, and knowledge bases.

This connector ingests workspaces, hierarchical document trees, rich Markdown page content, and database views (Table, Kanban, Calendar) into structured Markdown documents optimized for Cognee's `cognify()` knowledge graph pipeline and semantic vector retrieval.

---

## Key Capabilities

- **Workspace Discovery**: Extracts accessible workspaces, member counts, ownership metadata, and cloud/self-hosted endpoints.
- **Hierarchical Document Ingestion**: Ingests document trees, parent-child folder structures, Markdown text content, and revision timestamps.
- **Database & Table View Ingestion**: Ingests database views (Table, Kanban, Calendar), property field definitions, and structured record rows.
- **Self-Hosted & Cloud Compatibility**: Configurable `base_url` supporting both AppFlowy Cloud and private, self-hosted AppFlowy Server deployments.
- **Document-Mode Routing**: Sets `DOCUMENT_SOURCE_ATTR = "appflowy"` ensuring documents route through Cognee's downstream cognitive extraction.
- **Orphan Cleanup (`write_disposition="replace"`)**: Automatically syncs current state and removes deleted pages or database rows from Cognee storage.
- **Deterministic Document IDs**: Uses stable `uuid5` hashing so subsequent syncs idempotently update existing memory nodes without duplicate creation.
- **Security & Token Masking**: Automatically redacts API tokens (`[REDACTED_APPFLOWY_TOKEN]`) from logs, error messages, and exception traces.

---

## Installation

```bash
pip install cognee cognee-community-connector-appflowy
```

Or using `uv`:

```bash
uv add cognee cognee-community-connector-appflowy
```

---

## Configuration Reference

The connector is configured using `AppFlowyConfig`:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `api_key` | `str` | *required* | AppFlowy API Bearer token |
| `base_url` | `str` | `https://api.appflowy.cloud/api/v1` | AppFlowy Server API base URL |
| `workspace_ids` | `list[str] \| None` | `None` | Specific workspace IDs to ingest (or `None` for all accessible) |
| `include_documents` | `bool` | `True` | Whether to extract documents, pages, and hierarchical text content |
| `include_databases` | `bool` | `True` | Whether to extract database tables, Kanban views, and record rows |
| `limit` | `int` | `50` | Pagination limit for database records and documents |
| `request_timeout` | `float` | `30.0` | HTTP request timeout in seconds |

---

## Quickstart

```python
import asyncio
import os
import cognee
from cognee_community_connector_appflowy import (
    AppFlowyConfig,
    appflowy_connector,
)


async def main():
    config = AppFlowyConfig(
        api_key=os.getenv("APPFLOWY_API_KEY", "af_token_example"),
        base_url=os.getenv("APPFLOWY_BASE_URL", "https://api.appflowy.cloud/api/v1"),
        include_documents=True,
        include_databases=True,
    )

    # 1. Fetch AppFlowy documentation and databases
    documents = await appflowy_connector(config)
    print(f"Retrieved {len(documents)} AppFlowy knowledge documents.")

    # 2. Add to Cognee memory
    await cognee.add(documents, dataset_name="internal_knowledge")

    # 3. Cognify into knowledge graph
    await cognee.cognify(dataset_name="internal_knowledge")

    # 4. Query team documentation and project databases
    query = "What are the key technical specifications in the architecture RFC?"
    results = await cognee.search(query, dataset_name="internal_knowledge")
    for r in results:
        print(f"Memory match: {r}")


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Running Offline Tests

The connector includes comprehensive offline unit tests that run with synthetic mocked HTTP transports (no live AppFlowy account or network connection required):

```bash
uv run --project packages/connector/appflowy --with pytest --with pytest-asyncio pytest packages/connector/appflowy/tests -p no:typeguard -v
```

---

## License

Apache-2.0
