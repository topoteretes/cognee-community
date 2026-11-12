# GDELT DOC 2.0 Global News Metadata Connector for Cognee

A production-ready data-source connector for **[GDELT Project](https://www.gdeltproject.org/)** (DOC 2.0 API).

Extracts continuously updated worldwide news coverage, publisher domains, language tags, and event timelines into Cognee's semantic memory graph.

## Features

- **Worldwide News Event Ingestion**: Queries the unauthenticated GDELT DOC 2.0 ArticleList JSON API to index global coverage across hundreds of languages.
- **Copyright & Provenance Safety**: Indexes article headlines, publisher domains, source country codes, publication timestamps, and direct URLs without violating copyright boundaries by scraping third-party article bodies.
- **Timestamp Normalization**: Automatically converts GDELT `YYYYMMDDHHMMSS` date strings into human-readable ISO format.
- **Graph Grouping without LLM Processing**: Generates `cognee_node_set` entries for search queries (`gdelt:query:...`), publisher domains (`gdelt:domain:...`), and countries (`gdelt:country:...`).
- **Offline Snapshot Support**: Full support for offline JSON snapshots for deterministic testing and reproducible benchmarks.
- **DLT Full-Snapshot Replace Sync**: Guaranteed idempotent updates with SHA-256 document hashing (`DOCUMENT_SOURCE_ATTR = "gdelt"`).

## Installation

```bash
pip install cognee-community-connector-gdelt
```

## Quick Start

### 1. Ingest via Cognee Python API

```python
import cognee
from cognee_community_connector_gdelt import gdelt_source

# Option A: Live GDELT query
source = gdelt_source(
    query="artificial intelligence AND robotics",
    max_records=50,
)

# Option B: Offline JSON snapshot file
source = gdelt_source(snapshot_data="/path/to/gdelt_snapshot.json")

# Ingest into Cognee
cognee.add(source)
cognee.cognify()
```

### 2. Querying Agent Memory

Once cognified, agents can answer queries such as:
- *"Which international news sources first reported on recent breakthroughs in autonomous agents?"*
- *"Show all recent news articles published in Japan regarding renewable energy."*
- *"Summarize the timeline of global reporting on Base network adoption."*

## Testing

Run unit tests hermetically:

```bash
pytest packages/connector/gdelt/tests/ -v
```
