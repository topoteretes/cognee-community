# PyPI Data-Source Connector for Cognee

A community-contributed data-source connector for **Cognee** (the open-source AI memory platform) that ingests package metadata, dependencies, releases, and full documentation directly from the official **Python Package Index (PyPI)** REST API.

---

## Why this Connector?

When building developer assistants, code-generation agents, or research systems, agents often suffer from outdated APIs or library hallucination. 

This connector indexes real-time Python package specifications directly into Cognee's memory graph:
- Ingests package summaries, authors, licenses, and project links.
- Extracts complete README documentation (Markdown & reStructuredText).
- Tracks direct dependency requirements (`requires_dist`).
- Enables natural language queries across libraries: *"What dependencies does package X require?"* or *"How do I initialize an agent with library Y?"*

---

## Installation

```bash
pip install cognee-community-connector-pypi
```
Or with `uv`:
```bash
uv pip install cognee-community-connector-pypi
```

---

## Quickstart

```python
import asyncio
import cognee
from cognee_community_connector_pypi import PyPIConnector

async def main():
    connector = PyPIConnector()

    # 1. Fetch package documentation & metadata
    packages = ["cognee", "fastapi", "pydantic"]
    documents = await connector.fetch_documents(packages)

    # 2. Ingest into Cognee memory
    for doc in documents:
        await cognee.add(doc["text"])

    # 3. Build knowledge graph & embeddings
    await cognee.cognify()

    # 4. Search memory
    results = await cognee.search("What are the key dependencies and features of fastapi?")
    print(results)

if __name__ == "__main__":
    asyncio.run(main())
```

---

## Features

- **Zero-Authentication**: Connects directly to the public PyPI JSON API (`https://pypi.org/pypi/{package}/json`).
- **Async & Non-blocking**: Uses `httpx.AsyncClient` for high-throughput batch retrieval.
- **Resilient**: Gracefully handles 404 missing packages with configurable fallback options.
- **Structured Documents**: Standardized metadata headers (`package_name`, `version`, `license`, `author`) ready for graph entity linking.

---

## Running Tests

```bash
pytest tests/ -v
```
