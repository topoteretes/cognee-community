# BigCommerce E-commerce Connector for Cognee

A `dlt` data-source connector that ingests catalog products, SKU variants, inventory levels, and order fulfillment records from BigCommerce stores into Cognee memory for automated retail analytics and customer service agents.

Part of the **Cognee Mergetober Hackathon** (`#6273`).

---

## Features

- **Product Catalog & Variant Tracking:** Ingests product names, SKUs, pricing, stock levels, and multi-variant matrix configurations.
- **HTML Sanitization:** Converts rich HTML product descriptions into clean Markdown for high-quality embedding and semantic search.
- **Order Ingestion:** Ingests recent order histories, order statuses, totals, and fulfillment timelines.
- **Snapshot Replacement:** Configured with `write_disposition="replace"` for complete synchronization.
- **Document Mode:** Tags resources with `DOCUMENT_SOURCE_ATTR` to route directly through Cognee's `cognify` entity-extraction pipeline.

---

## Quickstart

```python
import asyncio
import os
import cognee
from cognee_community_connector_bigcommerce import bigcommerce_source

async def main():
    # 1. Initialize BigCommerce connector
    source = bigcommerce_source(
        store_hash=os.getenv("BIGCOMMERCE_STORE_HASH"),
        access_token=os.getenv("BIGCOMMERCE_ACCESS_TOKEN"),
        include_orders=True,
        limit=50,
    )

    # 2. Add to Cognee memory
    await cognee.add(source)

    # 3. Cognify into knowledge graph
    await cognee.cognify()

    # 4. Search retail intelligence
    results = await cognee.search("Which mechanical keyboards have stock available?")
    for res in results:
        print("-", res)

if __name__ == "__main__":
    asyncio.run(main())
```

---

## Running Tests

Run the hermetic offline test suite:

```bash
pytest packages/connector/bigcommerce/tests
```
