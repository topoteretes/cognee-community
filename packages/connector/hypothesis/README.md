# Hypothesis (hypothes.is) Connector for Cognee

A high-performance data-source connector for [Cognee](https://github.com/topoteretes/cognee) that ingests user web and PDF annotations, highlights, margin notes, and tags directly from the [Hypothesis REST API](https://h.readthedocs.io/en/latest/api-reference/) into Cognee's long-term memory graph.

---

## Features

- **Quote Extraction**: Extracts highlighted text quotes via `TextQuoteSelector`.
- **Margin Notes & Tags**: Ingests user notes and semantic hashtags.
- **Aggregated Page Documents**: Groups annotations by target URL into consolidated Markdown documents with deterministic `data_id` hashes to prevent unnecessary re-indexing.
- **Incremental & Paged Sync**: Supports pagination with `search_after` cursor traversal.
- **dlt Integration**: Implements `@dlt.source` and `@dlt.resource` with snapshot replace contracts.

---

## Installation

```bash
pip install -e packages/connector/hypothesis
```

---

## Quickstart

```python
import cognee
from cognee_community_connector_hypothesis import hypothesis_source

async def main():
    source = hypothesis_source(
        api_token="YOUR_HYPOTHESIS_API_TOKEN",
        username="myusername",
    )
    
    await cognee.add(source)
    await cognee.cognify()
    
    results = await cognee.search("What did I highlight about transformer attention mechanisms?")
    print(results)
```

---

## Authentication

Generate your personal developer API token from your Hypothesis account settings:
👉 [https://hypothes.is/account/developer](https://hypothes.is/account/developer)
