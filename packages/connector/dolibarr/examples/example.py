import asyncio
from cognee_community_connector_dolibarr import DolibarrConnector


async def main():
    # Replace with your self-hosted Dolibarr instance URL and API key
    base_url = "https://erp.example.com"
    api_key = "your_dolibarr_api_key_here"

    connector = DolibarrConnector(
        base_url=base_url,
        api_key=api_key,
        include_private_notes=False,
    )

    try:
        print("Ingesting Dolibarr ERP/CRM data (third-parties, contacts, proposals, tickets)...")
        documents = await connector.to_documents(
            include_thirdparties=True,
            include_contacts=True,
            include_proposals=True,
            include_tickets=True,
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
