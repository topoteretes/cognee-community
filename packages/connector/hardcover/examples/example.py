import asyncio
from cognee_community_connector_hardcover import HardcoverConnector


async def main():
    # Provide your Hardcover Personal Access Token (hc_pat_...)
    token = "hc_pat_sample_token_12345"

    connector = HardcoverConnector(token=token, include_private=False)

    try:
        print("Fetching user profile...")
        me = await connector.fetch_me()
        print(f"Connected to Hardcover user: {me.get('name', 'Reader')} (@{me.get('username', 'user')})")

        print("\nGenerating Cognee markdown documents from books, journals, and lists...")
        documents = await connector.to_documents(
            include_books=True,
            include_journals=True,
            include_lists=True,
            limit=10,
        )

        print(f"Generated {len(documents)} cognitive memory documents:")
        for idx, doc in enumerate(documents[:3], 1):
            print(f"\n--- Document {idx} Preview ---")
            print(doc[:300] + ("..." if len(doc) > 300 else ""))

    finally:
        await connector.close()


if __name__ == "__main__":
    asyncio.run(main())
