# Penpot Open-Source Design Connector for Cognee

The official `penpot` data-source connector for [Cognee](https://github.com/topoteretes/cognee), syncing design files, boards, UI copy / text layers, component instances, and team discussion threads from [Penpot](https://penpot.app/) (the open-source, self-hostable design platform) into AI agent memory and relationship graphs.

---

## Overview

Design tools hold crucial product and technical context: screen architectures, UI copy, reusable component usage, and the team discussion threads where UX decisions were made.

This connector interfaces with the Penpot RPC API (on `design.penpot.app` or self-hosted instances) using personal access tokens, ingesting design pages and comment threads directly into Cognee's document model (`cognify()`).

Security, design, and engineering agents can answer:
- *"Which screens in the mobile app currently use the `Controls/SwipeSliderPrimary` component?"*
- *"Why was the checkout flow subtitle updated to mention Base paymaster sponsorship?"*
- *"List all unresolved design discussion threads across the checkout redesign project."*

### Key Capabilities

- **Cloud & Self-Hosted Penpot**: Connects to `design.penpot.app` or any self-hosted Penpot instance via `Authorization: Token <token>`.
- **Text & Component Allowlist**: Ingests UI copy, board hierarchies, and component instances without bloating the graph with heavy binary shapes or image assets.
- **Discussion Thread Tracking**: Ingests team comment threads, author names, reply timestamps, and resolution badges (`RESOLVED` vs `ACTIVE / OPEN`).
- **Document-Mode Routing**: Declares `DOCUMENT_SOURCE_ATTR = "penpot"` to enable entity extraction and cross-component relationship graphs.
- **Reconciliation & Orphan Cleanup**: Employs `write_disposition="replace"`, allowing deleted design files and threads to be removed automatically by Cognee's `orphan_cleanup`.

---

## Installation

```bash
pip install "cognee-community-connector-penpot"
```

Or from the `cognee-community` monorepo:

```bash
cd packages/connector/penpot
pip install -e .
```

---

## Quickstart

### 1. Ingest Penpot Designs into Cognee

```python
import asyncio
import os
import cognee
from cognee_community_connector_penpot import penpot_source


async def main():
    # 1. Initialize connector with Penpot Access Token
    source = penpot_source(
        auth_token=os.environ.get("PENPOT_TOKEN"),
        base_url="https://design.penpot.app",
        project_ids=["proj-uuid-1234"],
    )

    # 2. Ingest into Cognee staging
    await cognee.add(source)

    # 3. Cognify: extract entities, relationships, and vector embeddings
    await cognee.cognify()

    # 4. Search agent memory
    results = await cognee.search(
        "Which screens use our primary payment button component and what did the team discuss about it?"
    )
    for res in results:
        print(res)


if __name__ == "__main__":
    asyncio.run(main())
```

---

### 2. Offline / Local Snapshot Ingestion

```python
source = penpot_source(
    snapshot_data="./penpot_export_snapshot.json",
)
```

---

## Document Schema & Metadata

Each page and discussion thread is ingested as a structured Markdown document:

| Field | Type | Description |
|---|---|---|
| `id` | `str` | Canonical identifier (`penpot_page_<file>_<page>` or `penpot_thread_<file>_<thread>`) |
| `title` | `str` | Standardized header `[Penpot Design] <File> / <Page>` |
| `url` | `str` | Direct link to the Penpot workspace screen or thread |
| `file_name` | `str` | Name of the design file |
| `project_name` | `str` | Name of the enclosing project |
| `board_count` | `int` | Number of screens/boards on page |
| `component_count` | `int` | Number of reusable components used |
| `cognee_node_set` | `list[str]` | Scoped tags (`penpot`, `file:<name>`, `design`, `ui_components`) |
| `content` | `str` | Markdown body with board structure, UI copy, and component listings |

---

## Running Tests

Run the hermetic test suite:

```bash
PYTHONPATH=packages/connector/penpot pytest packages/connector/penpot/tests/ -v
```
