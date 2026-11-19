import asyncio
from cognee_community_connector_openml import OpenMLConnector

async def main():
    # Example: Ingest metadata for well-known OpenML tabular benchmarks
    # 61: Iris, 31: German Credit, 54: Hepatitis
    connector = OpenMLConnector(
        dataset_ids=[61, 31, 54],
        include_qualities=True
    )

    print("Fetching OpenML benchmark dataset metadata...")
    datasets = await connector.fetch_all()
    print(f"Successfully retrieved {len(datasets)} benchmark dataset records.\n")

    for ds in datasets:
        print(f"Dataset: {ds['name']} (ID: {ds['dataset_id']})")
        print(f"Status: {ds['status']}")
        print(f"Target Attribute: {ds['target_attribute']}")
        print(f"Reported Qualities: {ds['qualities']}")
        print("-" * 50)

if __name__ == "__main__":
    asyncio.run(main())
