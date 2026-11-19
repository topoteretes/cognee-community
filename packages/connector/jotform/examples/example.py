import asyncio
from cognee_community_connector_jotform import JotformConnector


async def main():
    api_key = "your_jotform_api_key_here"

    connector = JotformConnector(
        api_key=api_key,
        exclude_payment_fields=True,
    )

    try:
        print("Ingesting Jotform schemas and submissions into Cognee...")
        documents = await connector.to_documents(limit_per_form=10)

        print(f"Generated {len(documents)} Cognee documents:")
        for idx, doc in enumerate(documents[:3], 1):
            print(f"\n--- Document {idx} Preview ---")
            print(doc[:300] + ("..." if len(doc) > 300 else ""))

    finally:
        await connector.close()


if __name__ == "__main__":
    asyncio.run(main())
