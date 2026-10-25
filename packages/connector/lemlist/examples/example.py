import asyncio
from cognee_community_connector_lemlist import LemlistConnector


async def main():
    api_key = "your_lemlist_api_key_here"

    connector = LemlistConnector(api_key=api_key)

    try:
        print("Ingesting lemlist outreach campaigns and engagement statistics...")
        documents = await connector.to_documents(limit=10, include_stats=True)

        print(f"Generated {len(documents)} Cognee documents:")
        for idx, doc in enumerate(documents[:3], 1):
            print(f"\n--- Document {idx} Preview ---")
            print(doc[:300] + ("..." if len(doc) > 300 else ""))

    finally:
        await connector.close()


if __name__ == "__main__":
    asyncio.run(main())
