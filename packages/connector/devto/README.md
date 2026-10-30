# Cognee Community Connector: DEV.to (Forem)

A data source connector for [Cognee](https://github.com/topoteretes/cognee) that extracts developer articles, tutorials, engineering posts, and community knowledge from the [DEV.to (Forem) REST API](https://developers.forem.com/api/v1) into Cognee's AI memory graph.

## Features

- **Topic & Tag Ingestion:** Ingest technical discussions and tutorials filtered by tags (e.g. `python`, `ai`, `webdev`, `architecture`).
- **Author Feeds:** Index all articles written by a specific developer or engineering organization.
- **Full Markdown Content:** Option to fetch complete article markdown bodies or summaries.
- **Knowledge-Graph Ready:** Converts articles into structured documents with rich author, tag, reaction, and publication metadata for Cognee's `cognify()` chunking and relationship extraction.
- **Dual Pipeline Support:** Compatible with both `await cognee.add()` and `await cognee.remember()` (DLT pipeline).

---

## Installation

Inside the `cognee-community` monorepo:

```bash
cd packages/connector/devto
poetry install
```

Or via pip:

```bash
pip install cognee-community-connector-devto
```

---

## Configuration

The DEV.to public API requires no authentication for reading public articles and tag feeds. For authenticated requests (e.g. personal reading lists or private articles), provide an API key via environment variable or argument:

```bash
export DEVTO_API_KEY="your-devto-api-key"
```

---

## Usage

### 1. Document Mode Ingestion (`cognee.add` + `cognee.cognify`)

```python
import asyncio
import os
import cognee
from cognee_community_connector_devto import fetch_devto_documents

os.environ["LLM_API_KEY"] = "your-openai-or-anthropic-key"

async def main():
    # 1. Fetch top articles tagged with 'python'
    documents = fetch_devto_documents(tag="python", per_page=10, fetch_full_content=False)

    # 2. Add into Cognee dataset
    await cognee.add(documents, dataset_name="devto_knowledge_base")

    # 3. Cognify into knowledge graph
    await cognee.cognify(datasets=["devto_knowledge_base"])

    # 4. Query AI Memory
    results = await cognee.search(
        "What are the best practices and tutorials shared for Python developers?",
        datasets=["devto_knowledge_base"]
    )
    print(results)

if __name__ == "__main__":
    asyncio.run(main())
```

### 2. DLT Source Mode (`cognee.remember`)

```python
import asyncio
import cognee
from cognee_community_connector_devto import devto_source

async def main():
    source = devto_source(tag="ai", per_page=20)
    await cognee.remember(source, dataset_name="devto_ai_memory")

if __name__ == "__main__":
    asyncio.run(main())
```

---

## Running Tests

Run the test suite using pytest:

```bash
pytest packages/connector/devto/tests
```

---

## License

This package is licensed under the Apache 2.0 License, consistent with Cognee Community guidelines.
