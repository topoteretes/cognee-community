# Fern API Documentation Connector for Cognee

[![Python 3.10+](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue.svg)](https://www.python.org/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-yellow.svg)](https://opensource.org/licenses/Apache-2.0)
[![Code Style: Ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)
[![Mergetober Sprint](https://img.shields.io/badge/Mergetober-Sprint%202026-orange.svg)](https://github.com/topoteretes/cognee)

A high-performance `dlt` data-source connector for [Cognee](https://github.com/topoteretes/cognee) that transforms [Fern](https://buildwithfern.com) API definitions and documentation into persistent AI memory and interconnected knowledge graphs.

---

## Architecture Overview

Flagship AI and developer platforms (such as Cohere, ElevenLabs, Scale AI, and AssemblyAI) use Fern to manage API definitions and generate SDKs.

The **Fern Connector** ingests these structured definitions into Cognee's persistent graph memory:

```
┌────────────────────────────────┐
│      Fern API Repository       │
│ (fern.config.json + definition)│
└───────────────┬────────────────┘
                │ (Endpoints, Types, Parameters)
                ▼
┌────────────────────────────────┐
│      fern_source() (dlt)       │
│  - Endpoint Schema Parsing     │
│  - Guide Markdown Extraction   │
│  - Replace Snapshot Sync       │
└───────────────┬────────────────┘
                │ DOCUMENT_SOURCE_ATTR = "fern"
                ▼
┌────────────────────────────────┐
│    Cognee Pipeline Engine      │
│    await cognee.add(source)    │
│    await cognee.cognify()      │
└───────────────┬────────────────┘
                │ Entity & Relation Extraction
                ▼
┌────────────────────────────────┐
│    Persistent Graph Memory     │ ──> AI Agents, API Copilots
│  (Entities, Nodes & Vectors)   │     & Developer Support Bots
└────────────────────────────────┘
```

---

## Features

- **Document-Mode Ingestion:** Declares `DOCUMENT_SOURCE_ATTR = "fern"`, routing API schemas into Cognee's `cognify` entity-extraction pipeline.
- **Deep Endpoint Parsing:** Discovers and formats HTTP methods, URL paths, path parameters, query parameters, request bodies, response models, and error types.
- **Guide Integration:** Ingests markdown documentation and guides in `pages/` alongside API schemas.
- **Full Snapshot + Forget-on-Delete:** Uses `write_disposition="replace"`. Deleting or deprecating an endpoint automatically prunes obsolete graph nodes via Cognee's `orphan_cleanup`.
- **Zero API Keys Required:** Runs 100% offline with isolated unit tests.

---

## Installation

```bash
# In your project or cognee-community workspace
pip install -e packages/connector/fern
```

---

## Quickstart

```python
import asyncio
import cognee
from cognee_community_connector_fern import fern_source

async def main():
    # 1. Point to your Fern directory (containing fern.config.json and definition/)
    source = fern_source(fern_path="./fern")

    # 2. Add API definitions to Cognee
    print("Ingesting Fern API definitions...")
    await cognee.add(source)

    # 3. Extract knowledge graph entities and relations
    print("Cognifying API schemas...")
    await cognee.cognify()

    # 4. Search and retrieve insights from the graph
    insights = await cognee.search(
        query_type="INSIGHTS",
        query_text="What parameters are required to generate model completions?",
    )
    print("Knowledge Graph Insights:\n", insights)

if __name__ == "__main__":
    asyncio.run(main())
```

---

## Running Tests

```bash
# Run pytest test suite
pytest packages/connector/fern/tests/ -v

# Run linter
ruff check packages/connector/fern/
```

---

## Mergetober Hackathon

Built with ❤️ for **Mergetober** (WeMakeDevs x Hacker's Unity x Cognee).
