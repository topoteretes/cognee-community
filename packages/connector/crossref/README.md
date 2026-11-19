# Crossref DOI Metadata Connector for Cognee

A production-ready data-source connector for **[Crossref](https://www.crossref.org/)** (Works API).

Extracts scholarly publication records, author attribution, container venues, citation counts, and deposited open-access licenses into Cognee's semantic memory graph.

## Features

- **Scholarly DOI Metadata Extraction**: Queries Crossref's public `/works/{doi}` API to retrieve paper titles, author rosters with ORCID identifiers, publication venues, dates, and citation metrics.
- **Canonical DOI Normalization**: Strips `https://doi.org/`, `http://dx.doi.org/`, and `doi:` prefixes into clean lowercase canonical strings.
- **Open-Access License Tracking**: Captures deposited open-access and author-rights licenses directly into the metadata graph.
- **Graph Grouping without LLM Processing**: Automatically generates `cognee_node_set` entries for DOIs (`crossref:doi:...`), publication venues (`crossref:venue:...`), and author family names (`crossref:author:...`).
- **Offline Snapshot Support**: Supports offline JSON snapshots for reproducible benchmarks and deterministic CI.
- **DLT Full-Snapshot Replace Sync**: Guaranteed idempotent updates with SHA-256 document hashing (`DOCUMENT_SOURCE_ATTR = "crossref"`).

## Installation

```bash
pip install cognee-community-connector-crossref
```

## Quick Start

### 1. Ingest via Cognee Python API

```python
import cognee
from cognee_community_connector_crossref import crossref_source

# Option A: Live DOI lookup
source = crossref_source(
    dois=[
        "10.1145/3442188.3445922",
        "10.1038/nature12373",
    ],
    mailto="research@your-domain.org",  # Polite API pool
)

# Option B: Offline JSON snapshot file
source = crossref_source(snapshot_data="/path/to/crossref_snapshot.json")

# Ingest into Cognee
cognee.add(source)
cognee.cognify()
```

### 2. Querying Agent Memory

Once cognified, agents can answer queries such as:
- *"Which papers in our reading list were published in ACM FAccT?"*
- *"Show all co-authored works by Emily Bender in our research memory."*
- *"Which indexed publications have deposited open-access licenses?"*

## Testing

Run unit tests hermetically:

```bash
pytest packages/connector/crossref/tests/ -v
```
