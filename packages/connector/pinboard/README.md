# Cognee Community Connector: Pinboard

Ingest bookmarks, tags, reading lists, and notes from **Pinboard** into Cognee's AI Memory & Knowledge Graph.

## Features
- **Bookmark & Metadata Ingestion:** Ingests titles, canonical URLs, extended notes, timestamps, and tags.
- **Reading List Tracking:** Captures `toread` flags for intelligent backlog surfacing by AI agents.
- **Deterministic Deduplication:** SHA-256 canonical URL hashing ensures idempotent synchronizations.

## Installation
```bash
pip install -e packages/connector/pinboard
```

## Quick Start
```python
from cognee_community_connector_pinboard import pinboard_source
import cognee

# Ingest all bookmarks
documents = pinboard_source(
    auth_token="username:YOUR_SECRET_TOKEN"
)

# Remember into Cognee Knowledge Graph
await cognee.remember(documents)
```

## Authentication
Find your API token under **Pinboard Settings -> Password**:
- Format: `username:TOKEN`

## Running Tests
```bash
PYTHONPATH=packages/connector/pinboard pytest packages/connector/pinboard/tests -v
```
