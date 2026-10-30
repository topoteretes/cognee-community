# cognee-community-connector-substack

Substack newsletter data-source connector for [cognee](https://github.com/topoteretes/cognee).

Ingests published Substack newsletter essays, articles, and thought leadership into cognee memory as interconnected documents.

## Usage

```python
import cognee
from cognee_community_connector_substack import substack_source

# Ingest posts by publication name or full URL
await cognee.remember(
    substack_source(["pragmaticengineer", "astralcodexten"]),
    dataset_name="tech-newsletters",
)
```

## Features

- **Subdomain & URL resolution**: Accepts publication handles (e.g. `"pragmatic"`) or custom domain URLs.
- **HTML to clean markdown**: Transforms nested HTML elements, headings, and blockquotes into clean markdown.
- **Author and publication attribution**: Tracks publication titles, post authors, published dates, and canonical links.
