# Gumroad Connector for Cognee

A native creator storefront data-source connector for [Cognee](https://github.com/topoteretes/cognee), syncing digital product catalogs, descriptions, pricing tiers, variants, and storefront URLs directly into Cognee's AI knowledge graph.

## Features

- **Storefront Catalog Sync**: Ingests digital products, courses, ebooks, software tools, and templates from your Gumroad account.
- **Rich Semantic Documents**: Generates structured product profiles with clean text descriptions (HTML stripped), pricing tiers, variant breakdowns, and product tags for optimal knowledge graph extraction.
- **Native dlt Integration**: Standard `dlt.source` implementation with deterministic primary keys (`gumroad:product:<product_id>`) and `merge` write disposition to prevent duplicate product records.
- **Resilient Rate Limiting**: Built-in exponential backoff for HTTP 429 rate limits.

## Installation

Add the connector to your Cognee environment:

```bash
pip install cognee-community-connector-gumroad
```

Or using `uv`:

```bash
uv add cognee-community-connector-gumroad
```

## Quick Start

### 1. Ingesting Storefront Products into Cognee

```python
import asyncio
import os
import cognee
from cognee_community_connector_gumroad import gumroad_source

async def main():
    # Ingest Gumroad catalog
    await cognee.remember(
        gumroad_source(
            access_token=os.getenv("GUMROAD_ACCESS_TOKEN"),
        ),
        dataset_name="creator_storefront",
    )

    # Build knowledge graph
    await cognee.cognify(datasets=["creator_storefront"])

    # Query your store knowledge
    results = await cognee.search(
        "What are the features and pricing tiers of the AI Playbook?",
        query_type="INSIGHTS",
    )
    for insight in results:
        print(insight)

if __name__ == "__main__":
    asyncio.run(main())
```

### 2. Standalone Product Extraction

```python
from cognee_community_connector_gumroad import GumroadClient, fetch_gumroad_products

client = GumroadClient(access_token="your_token_here")

for product in fetch_gumroad_products(client=client):
    print(f"[{product['name']}] Price: {product['formatted_price']} | URL: {product['url']}")
```

## Data Schema

Each ingested record produces a row in the `gumroad_products` resource table:

| Field | Type | Description |
|---|---|---|
| `id` | `str` | Primary key (`gumroad:product:<product_id>`) |
| `product_id` | `str` | Gumroad product alphanumeric identifier |
| `name` | `str` | Product title |
| `description` | `str` | Plain text product description (HTML tags stripped) |
| `price_cents` | `int` | Integer price in cents (e.g. `1900` = $19.00) |
| `price_decimal` | `float` | Fractional currency price (e.g. `19.0`) |
| `formatted_price` | `str` | Formatted store price string (e.g. `"$19"`) |
| `currency` | `str` | Store currency code (e.g. `"usd"`) |
| `url` | `str` | Direct store product permalink |
| `tags` | `list[str]` | Seller product categorization tags |
| `published` | `bool` | Whether product is live on storefront |
| `timestamp` | `int` | Unix epoch ingestion timestamp |
| `text` | `str` | Structured text for knowledge graph embedding |

## Authentication

Create a Gumroad Access Token:
1. Log in to [Gumroad Settings > Advanced](https://gumroad.com/settings/advanced).
2. Create an Application or generate a Personal Access Token with `view_profile` scope.
3. Export your token: `export GUMROAD_ACCESS_TOKEN="..."`.

## License

Apache 2.0
