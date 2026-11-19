import asyncio
from cognee_community_connector_workable import WorkableConnector


async def main():
    api_key = "your_workable_api_key_here"
    subdomain = "your-company-subdomain"

    connector = WorkableConnector(
        api_key=api_key,
        subdomain=subdomain,
        include_candidate_pii=False,
    )

    try:
        print("Ingesting Workable hiring requisitions and candidate pipelines...")
        documents = await connector.to_documents(state="published", limit=10, include_candidates=True)

        print(f"Generated {len(documents)} Cognee documents:")
        for idx, doc in enumerate(documents[:3], 1):
            print(f"\n--- Document {idx} Preview ---")
            print(doc[:300] + ("..." if len(doc) > 300 else ""))

    finally:
        await connector.close()


if __name__ == "__main__":
    asyncio.run(main())
