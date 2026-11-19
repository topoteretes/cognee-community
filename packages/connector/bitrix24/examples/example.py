import asyncio
from cognee_community_connector_bitrix24 import Bitrix24Connector


async def main():
    # Replace with your actual Bitrix24 inbound webhook endpoint
    webhook_url = "https://your-company.bitrix24.com/rest/1/abcdef1234567890"

    connector = Bitrix24Connector(webhook_url=webhook_url)

    try:
        print("Fetching Bitrix24 CRM entities (deals, contacts, companies)...")
        documents = await connector.to_documents(
            crm_types=["deal", "contact", "company"],
            limit_per_type=10,
            include_comments=True,
        )

        print(f"Generated {len(documents)} markdown documents for Cognee memory ingestion:")
        for idx, doc in enumerate(documents[:3], 1):
            print(f"\n--- Document {idx} Preview ---")
            print(doc[:300] + ("..." if len(doc) > 300 else ""))

    finally:
        await connector.close()


if __name__ == "__main__":
    asyncio.run(main())
