import asyncio
from cognee_community_connector_meilisearch import MeilisearchConnector

async def main():
    connector = MeilisearchConnector(
        host="http://localhost:7700",
        api_key="your_meilisearch_api_key",
        indexes=["movies", "products"],
        limit_per_index=20,
    )

    print("Extracting index documents from Meilisearch...")
    documents = await connector.fetch_all()
    print(f"Retrieved {len(documents)} searchable documents.\n")

    for doc in documents[:5]:
        print(f"Index: {doc['index_uid']} | ID: {doc['doc_id']}")
        print(f"Preview: {doc['clean_text'][:120]}...\n")

if __name__ == "__main__":
    asyncio.run(main())
