# Docusaurus Documentation Connector for Cognee

[![Python 3.10+](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue.svg)](https://www.python.org/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-yellow.svg)](https://opensource.org/licenses/Apache-2.0)
[![Code Style: Ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)
[![Mergetober Sprint](https://img.shields.io/badge/Mergetober-Sprint%202026-orange.svg)](https://github.com/topoteretes/cognee)

A high-performance `dlt` data-source connector for [Cognee](https://github.com/topoteretes/cognee) that transforms [Docusaurus](https://docusaurus.io) documentation websites into persistent AI memory and interconnected knowledge graphs.

---

## Architecture Overview

Flagship open-source projects and developer platforms (such as React, Supabase, Redis, Algolia, and Babel) use Docusaurus to publish documentation.

The **Docusaurus Connector** bridges these static documentation sites directly into Cognee's persistent memory:

```
┌────────────────────────────────┐
│   Docusaurus Site Directory    │
│ (sidebars.json + docs/*.mdx)   │
└───────────────┬────────────────┘
                │ (Sidebar Hierarchy & Categories)
                ▼
┌────────────────────────────────┐
│   docusaurus_source() (dlt)    │
│  - Admonition Converter        │
│  - Frontmatter Extraction      │
│  - Replace Snapshot Sync       │
└───────────────┬────────────────┘
                │ DOCUMENT_SOURCE_ATTR = "docusaurus"
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

- **Document-Mode Ingestion:** Declares `DOCUMENT_SOURCE_ATTR = "docusaurus"`, routing documentation pages into Cognee's `cognify` entity-extraction pipeline.
- **Sidebar Hierarchy:** Automatically walks `sidebars.json` categories and nested items, attaching navigation breadcrumbs (`Sidebar > Category > Page`).
- **Admonition Parser:** Translates Docusaurus custom admonition blocks (`:::note`, `:::tip`, `:::info`, `:::caution`, `:::danger`) into semantic Markdown callouts.
- **Full Snapshot + Forget-on-Delete:** Uses `write_disposition="replace"`. Deleting or unlinking a documentation page automatically prunes obsolete graph nodes via Cognee's `orphan_cleanup`.
- **Zero API Keys Required:** Runs 100% offline with isolated unit tests.

---

## Installation

```bash
# In your project or cognee-community workspace
pip install -e packages/connector/docusaurus
```

---

## Quickstart

```python
import asyncio
import cognee
from cognee_community_connector_docusaurus import docusaurus_source

async def main():
    # 1. Point to your Docusaurus directory (containing docs/ and sidebars.json)
    source = docusaurus_source(site_path="./path/to/docusaurus-site")

    # 2. Add documentation pages to Cognee
    print("Ingesting Docusaurus documentation...")
    await cognee.add(source)

    # 3. Extract knowledge graph entities and relations
    print("Cognifying documents...")
    await cognee.cognify()

    # 4. Search and retrieve insights from the graph
    insights = await cognee.search(
        query_type="INSIGHTS",
        query_text="What are the prerequisite setup steps and configuration options?",
    )
    print("Knowledge Graph Insights:\n", insights)

if __name__ == "__main__":
    asyncio.run(main())
```

---

## Running Tests

```bash
# Run pytest test suite
pytest packages/connector/docusaurus/tests/ -v

# Run linter
ruff check packages/connector/docusaurus/
```

---

## Mergetober Hackathon

Built with ❤️ for **Mergetober** (WeMakeDevs x Hacker's Unity x Cognee).
