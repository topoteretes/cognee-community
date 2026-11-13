# cognee-community-connector-arxiv

arXiv research papers data-source connector for [cognee](https://github.com/topoteretes/cognee).

Ingests scientific paper abstracts, titles, authors, and metadata from the arXiv API into cognee memory.

## Usage

```python
import cognee
from cognee_community_connector_arxiv import arxiv_source

# Ingest top recent AI papers
await cognee.remember(
    arxiv_source(category="cs.AI", max_results=30),
    dataset_name="arxiv-ai-research",
)
```

## Features

- **Category & Query filters**: Search across subjects (`cs.AI`, `cs.LG`, `quant-ph`, etc.) or freeform search queries.
- **Respectful rate limiting**: Automatically manages request cadence to honor arXiv's 3-second request policy.
- **Rich metadata parsing**: Extracts titles, full author lists, primary categories, publication dates, and direct PDF links.
