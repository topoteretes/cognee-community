# cognee-community-connector-pubmed

PubMed biomedical literature data-source connector for [cognee](https://github.com/topoteretes/cognee).

Ingests scientific medical abstracts, clinical trial reports, and PubMed citations from the NCBI E-utilities API into cognee memory.

## Usage

```python
import cognee
from cognee_community_connector_pubmed import pubmed_source

# Ingest top articles on target medical query
await cognee.remember(
    pubmed_source(query="immunotherapy AND melanoma", max_results=25),
    dataset_name="biomedical-oncology",
)
```

## Features

- **NCBI E-utilities pipeline**: Coordinates `esearch` query discovery and `efetch` full XML retrieval.
- **Structured abstract parsing**: Preserves sections (Background, Methods, Results, Conclusions).
- **Author & citation extraction**: Formats authors, journal name, year, PMID, and direct DOI resolvers.
