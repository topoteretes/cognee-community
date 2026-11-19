import asyncio
from cognee_community_connector_woocommerce import WooCommerceConnector


async def main():
    store_url = "https://your-woocommerce-store.com"
    consumer_key = "ck_your_consumer_key"
    consumer_secret = "cs_your_consumer_secret"

    connector = WooCommerceConnector(
        store_url=store_url,
        consumer_key=consumer_key,
        consumer_secret=consumer_secret,
        include_customer_pii=False,
    )

    try:
        print("Ingesting WooCommerce catalog products and customer orders...")
        documents = await connector.to_documents(
            include_products=True,
            include_orders=True,
            limit_per_type=10,
        )

        print(f"Generated {len(documents)} Cognee documents:")
        for idx, doc in enumerate(documents[:3], 1):
            print(f"\n--- Document {idx} Preview ---")
            print(doc[:300] + ("..." if len(doc) > 300 else ""))

    finally:
        await connector.close()


if __name__ == "__main__":
    asyncio.run(main())
