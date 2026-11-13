# Mintlify Documentation Connector for Cognee

[![Python 3.10+](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue.svg)](https://www.python.org/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-yellow.svg)](https://opensource.org/licenses/Apache-2.0)
[![Code Style: Ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)
[![Mergetober Sprint](https://img.shields.io/badge/Mergetober-Sprint%202026-orange.svg)](https://github.com/topoteretes/cognee)

A high-performance `dlt` data-source connector for [Cognee](https://github.com/topoteretes/cognee) that transforms [Mintlify](https://mintlify.com) documentation hubs into persistent AI memory and interconnected knowledge graphs.

---

## Architecture Overview

Modern developer platforms, AI tools, and APIs (such as Anthropic, Cursor, Pinecone, Resend, ElevenLabs, and Perplexity) rely on Mintlify as their official documentation engine. 

The **Mintlify Connector** turns static doc repos into a living knowledge graph:

```
┌──────────────────────────────┐
│  Mintlify Repo / Directory   │
│ (mint.json + .mdx/.md files) │
└──────────────┬───────────────┘
               │ (Navigation Hierarchy & Breadcrumbs)
               ▼
┌──────────────────────────────┐
│   mintlify_source() (dlt)    │
│  - Frontmatter Extraction    │
│  - MDX Component Cleaning    │
│  - Replace Snapshot Sync     │
└──────────────┬───────────────┘
               │ DOCUMENT_SOURCE_ATTR = "mintlify"
               ▼
┌──────────────────────────────┐
│   Cognee Pipeline Engine     │
│   await cognee.add(source)   │
│   await cognee.cognify()     │
└──────────────┬───────────────┘
               │ Entity & Relation Extraction
               ▼
┌──────────────────────────────┐
│   Persistent Graph Memory    │ ──> AI Agents, IDE Copilots
│ (Entities, Nodes & Vectors)  │     & Developer Support Bots
└──────────────────────────────┘
```

---

## Features

- **Document-Mode Ingestion:** Declares `cognee_document_source = "mintlify"`, routing documentation pages directly into Cognee's natural language `cognify` entity-extraction pipeline.
- **Hierarchy & Breadcrumbs:** Discovers and parses `mint.json` tabs, nested groups, and page sequences, preserving navigational context (`Group > Subgroup > Page`).
- **MDX Tag Sanitizer:** Converts custom Mintlify JSX components (`<Card>`, `<ParamField>`, `<ResponseField>`, `<Accordion>`, `<Tip>`, `<Warning>`, `<Note>`) into clean, LLM-optimized semantic Markdown while preserving code blocks and API paths.
- **Full Snapshot + Forget-on-Delete:** Uses `write_disposition="replace"` with content-hash identity. When a documentation page is removed or renamed upstream, Cognee's `orphan_cleanup` deletes the stale graph nodes automatically.
- **Zero API Keys Required:** Ingests directly from local directories or cloned documentation repositories. 100% offline unit tests.

---

## Installation

```bash
# In your project or cognee-community workspace
pip install -e packages/connector/mintlify
```

Or install with development dependencies:

```bash
pip install -e "packages/connector/mintlify[dev]"
```

---

## Quickstart

```python
import asyncio
from pathlib import Path
import cognee
from cognee_community_connector_mintlify import mintlify_source

async def main():
    # 1. Point to your Mintlify documentation directory (containing mint.json)
    source = mintlify_source(docs_path="./path/to/docs")

    # 2. Ingest documentation pages into Cognee
    print("Ingesting Mintlify documentation into Cognee...")
    await cognee.add(source)

    # 3. Extract knowledge graph entities and relations
    print("Cognifying documents...")
    await cognee.cognify()

    # 4. Search and retrieve insights from the graph
    insights = await cognee.search(
        query_type="INSIGHTS",
        query_text="What are the required headers and parameters for the authentication endpoint?",
    )
    print("Knowledge Graph Insights:\n", insights)

if __name__ == "__main__":
    asyncio.run(main())
```

---

## Running Tests

All unit tests use isolated temporary fixtures and require no external services:

```bash
# Run pytest test suite
pytest packages/connector/mintlify/tests/ -v

# Run linter
ruff check packages/connector/mintlify/
```

---

## Mergetober Hackathon

Built with ❤️ for **Mergetober** (WeMakeDevs x Hacker's Unity x Cognee).
