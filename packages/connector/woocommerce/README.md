# WooCommerce Self-Hosted Store Community Connector for Cognee

An enterprise REST API connector integrating self-hosted **WooCommerce** stores with **Cognee**. Ingest product catalogs, pricing variants, stock statuses, verified customer reviews, orders, item subtotals, and order notes into Cognee's cognitive memory graph.

## Features

- **Product Catalogs & Categories**: Ingests product names, SKUs, regular/sale prices, descriptions, and category hierarchies.
- **Verified Customer Reviews**: Attaches 5-star ratings, reviewer identities, and review comments directly under their corresponding product documents.
- **Order Lifecycle & Checkout Notes**: Ingests order totals, line items, fulfillment statuses, and customer delivery instructions.
- **Order Notes Timeline**: Ingests internal staff communications and customer-visible status updates.
- **Customer Privacy Safeguard**: Built-in PII protection (`include_customer_pii=False` by default) protects customer addresses, phone numbers, and payment tokens from vector storage.
- **Hermetic Pytest Suite**: 100% offline mock test suite covering products, reviews, orders, notes, and 401/403 authorization failures.

## Installation

```bash
pip install cognee-community-connector-woocommerce
```

## Quickstart

```python
import asyncio
from cognee_community_connector_woocommerce import WooCommerceConnector

async def main():
    connector = WooCommerceConnector(
        store_url="https://your-store.com",
        consumer_key="ck_your_consumer_key",
        consumer_secret="cs_your_consumer_secret",
        include_customer_pii=False
    )

    # Ingest catalog products and orders into Cognee
    documents = await connector.to_documents(
        include_products=True,
        include_orders=True,
        limit_per_type=25
    )

    # await cognee.add(documents)
    # await cognee.cognify()

    await connector.close()

if __name__ == "__main__":
    asyncio.run(main())
```

## Running Tests

Execute the hermetic unit tests offline:

```bash
pytest packages/connector/woocommerce/tests/test_woocommerce.py -v
```
