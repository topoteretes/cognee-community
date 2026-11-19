# Amazon Kindle Highlights Connector for Cognee

The official `kindle` data-source connector for [Cognee](https://github.com/topoteretes/cognee), syncing book highlights, user notes, and reading annotations from your Kindle (`My Clippings.txt`) into AI agent memory and relationship graphs.

---

## Overview

Avid readers accumulate years of highlights and notes on Kindle devices and apps. These annotations are exported as a single flat text file (`documents/My Clippings.txt`), making it difficult to search across books or synthesize insights.

This connector parses `My Clippings.txt`, groups annotations by book, resolves overlapping extended highlights, filters zero-content bookmarks, and formats each book into a location-ordered Markdown document for Cognee's document ingestion pipeline (`cognify()`).

Readers can ask questions like:
- *"What key ideas have I highlighted across all books regarding habit formation and systems?"*
- *"Synthesize the contrasting arguments about human cognitive biases from my reading library."*
- *"Find my personal notes from The Pragmatic Programmer regarding refactoring."*

### Key Capabilities

- **Zero-Dependency Parser**: Built purely on the Python standard library with no external API, account, or scraping required.
- **Resilient Formatting**: Handles UTF-8 BOM, Windows CRLF / Unix LF line endings, and multi-line notes.
- **Book-Level Consolidation**: Emits one structured Markdown document per book, retaining author, title, page numbers, Kindle location ranges, and timestamps.
- **Overlap Resolution**: Automatically merges extended highlights when a user pauses and extends an existing passage.
- **Note-to-Highlight Pairing**: Pairs personal user notes with their corresponding passage location.
- **Document-Mode Routing**: Declares `DOCUMENT_SOURCE_ATTR = "kindle"` to enable entity extraction, relationship graph construction, and vector search.
- **Snapshot & Orphan Cleanup**: Employs `write_disposition="replace"`, allowing deleted books or clippings to be cleaned up automatically by Cognee's `orphan_cleanup`.

---

## Installation

```bash
pip install "cognee-community-connector-kindle"
```

Or from the `cognee-community` monorepo:

```bash
cd packages/connector/kindle
pip install -e .
```

---

## Quickstart

### 1. Ingest Kindle Clippings into Cognee

Plug in your Kindle via USB, locate `documents/My Clippings.txt`, and pass the file path:

```python
import asyncio
import cognee
from cognee_community_connector_kindle import kindle_source


async def main():
    # 1. Initialize connector with your clippings file
    source = kindle_source(path="/media/user/Kindle/documents/My Clippings.txt")

    # 2. Ingest into Cognee staging
    await cognee.add(source)

    # 3. Cognify: extract entities, relationships, and vector embeddings
    await cognee.cognify()

    # 4. Search across your personal book memory
    results = await cognee.search(
        "What have I highlighted about systems versus goals across all my books?"
    )
    for res in results:
        print(res)


if __name__ == "__main__":
    asyncio.run(main())
```

---

### 2. In-Memory String Ingestion

```python
source = kindle_source(content=clippings_text)
```

---

## Document Schema & Metadata

Each book is ingested as a structured Markdown document:

| Field | Type | Description |
|---|---|---|
| `id` | `str` | Canonical slug identifier (e.g. `kindle_atomic_habits_3a4b5c6d`) |
| `title` | `str` | Standardized header `[Kindle] <Title> (<Author>)` |
| `url` | `str` | Local reference URI `kindle://book/<id>` |
| `book_title` | `str` | Parsed book title |
| `author` | `str` | Parsed author name |
| `highlight_count` | `int` | Number of distinct highlights |
| `note_count` | `int` | Number of user notes |
| `cognee_node_set` | `list[str]` | Scoped tags (`kindle`, `author:<slug>`, `book`, `highlights`) |
| `content` | `str` | Structured Markdown body with blockquotes, notes, and locations |

---

## Running Tests

Run the hermetic test suite:

```bash
PYTHONPATH=packages/connector/kindle pytest packages/connector/kindle/tests/ -v
```
