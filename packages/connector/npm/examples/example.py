import asyncio
from cognee_community_connector_npm import NpmConnector


async def main():
    # Specify packages you wish to track in Cognee memory
    tracked_packages = ["react", "express", "@angular/core"]

    connector = NpmConnector(packages=tracked_packages, include_readme=True)

    try:
        print(f"Fetching manifests and READMEs for: {', '.join(tracked_packages)}...")
        documents = await connector.to_documents()

        print(f"Generated {len(documents)} Cognee markdown documents:")
        for idx, doc in enumerate(documents, 1):
            print(f"\n--- Document {idx} Preview ---")
            print(doc[:300] + ("..." if len(doc) > 300 else ""))

    finally:
        await connector.close()


if __name__ == "__main__":
    asyncio.run(main())
