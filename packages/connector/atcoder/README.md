# Cognee Community Connector: AtCoder

A community data-source connector for [Cognee](https://github.com/topoteretes/cognee) that ingests competitive programming user submissions, problem difficulty models, and contest performance history from **AtCoder** and the **Kenkoooo AtCoder Problems API**.

Resolves: [`cognee#6406`](https://github.com/topoteretes/cognee/issues/6406).

---

## Features

- **Problem Attempt History:** Groups multiple submissions per problem (e.g. `WA, WA, AC`) into a single structured document, retaining execution time, language, points, and difficulty models.
- **Problem Difficulty Modeling:** Integrates Kenkoooo's difficulty ratings and problem titles directly alongside attempts.
- **Contest Performance Tracking:** Ingests official rated contest history, rank/place, and rating changes.
- **Cognify Ready:** Produces rich Markdown documents ready for Cognee semantic search, graph completion, and analytical question answering (e.g. *"What difficulty band do I fail most?"*).
- **Public & Rate-Limit Safe:** Uses public JSON endpoints (no API key required) with built-in 1-second request pacing and exponential backoff retry policies.
- **Safe Snapshotting:** Validates unknown handles to prevent accidental empty snapshots from clearing user memory.

---

## Installation

```bash
pip install cognee-community-connector-atcoder
```

---

## Usage

```python
import cognee
from cognee_community_connector_atcoder import atcoder_source

# Ingest data for one or more AtCoder handles
cognee.add(atcoder_source(handles=["tourist", "chokudai"]))
cognee.cognify()

# Ask questions about performance
results = cognee.search("Which problems above 1200 difficulty have I not solved yet?")
```
