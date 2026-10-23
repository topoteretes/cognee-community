# VitePress Documentation Connector for Cognee

[![Python 3.10+](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue.svg)](https://www.python.org/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-yellow.svg)](https://opensource.org/licenses/Apache-2.0)
[![Code Style: Ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)
[![Mergetober Sprint](https://img.shields.io/badge/Mergetober-Sprint%202026-orange.svg)](https://github.com/topoteretes/cognee)

A high-performance `dlt` data-source connector for [Cognee](https://github.com/topoteretes/cognee) that transforms [VitePress](https://vitepress.dev) documentation websites into persistent AI memory and interconnected knowledge graphs.

---

## Architecture Overview

Flagship developer tools and JavaScript frameworks (such as Vite, Vue.js, Rollup, Vitest, and Pinia) use VitePress to build fast documentation sites.

The **VitePress Connector** ingests these repositories directly into Cognee's persistent graph memory:

```
┌────────────────────────────────┐
│    VitePress Site Directory    │
│ (.vitepress/config + docs/*.md)│
└───────────────┬────────────────┘
                │ (Sidebar Hierarchy & Containers)
                ▼
┌────────────────────────────────┐
│     vitepress_source() (dlt)   │
│  - Container Parser            │
│  - Frontmatter Extraction      │
│  - Replace Snapshot Sync       │
└───────────────┬────────────────┘
                │ DOCUMENT_SOURCE_ATTR = "vitepress"
                ▼
┌────────────────────────────────┐
│    Cognee Pipeline Engine      │
│    await cognee.add(source)    │
│    await cognee.cognify()      │
└───────────────┬────────────────┘
                │ Entity & Relation Extraction
                ▼
┌────────────────────────────────┐
│    Persistent Graph Memory     │ ──> AI Coding Copilots,
│  (Entities, Nodes & Vectors)   │     IDE Agents & Support Bots
└────────────────────────────────┘
```

---

## Features

- **Document-Mode Ingestion:** Declares `DOCUMENT_SOURCE_ATTR = "vitepress"`, routing pages into Cognee's `cognify` entity-extraction pipeline.
- **Sidebar Hierarchy:** Automatically walks `.vitepress/config` sidebar groups, multi-sidebars, and item links, attaching navigation breadcrumbs.
- **Container Parser:** Translates VitePress custom containers (`::: tip`, `::: warning`, `::: info`, `::: danger`, `::: details`) into semantic Markdown callout blocks.
- **Full Snapshot + Forget-on-Delete:** Uses `write_disposition="replace"`. Deleting or moving a documentation page automatically prunes obsolete graph nodes via Cognee's `orphan_cleanup`.
- **Zero API Keys Required:** Runs 100% offline with isolated unit tests.

---

## Installation

```bash
# In your project or cognee-community workspace
pip install -e packages/connector/vitepress
```

---

## Quickstart

```python
import asyncio
import cognee
from cognee_community_connector_vitepress import vitepress_source

async def main():
    # 1. Point to your VitePress directory (containing .vitepress/)
    source = vitepress_source(site_path="./path/to/vitepress-site")

    # 2. Add documentation pages to Cognee
    print("Ingesting VitePress documentation...")
    await cognee.add(source)

    # 3. Extract knowledge graph entities and relations
    print("Cognifying documents...")
    await cognee.cognify()

    # 4. Search and retrieve insights from the graph
    insights = await cognee.search(
        query_type="INSIGHTS",
        query_text="What are the prerequisite setup steps and build configurations?",
    )
    print("Knowledge Graph Insights:\n", insights)

if __name__ == "__main__":
    asyncio.run(main())
```

---

## Running Tests

```bash
# Run pytest test suite
pytest packages/connector/vitepress/tests/ -v

# Run linter
ruff check packages/connector/vitepress/
```

---

## Mergetober Hackathon

Built with ❤️ for **Mergetober** (WeMakeDevs x Hacker's Unity x Cognee).
