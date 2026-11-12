"""Example: Ingesting HydraDB contextual knowledge into Cognee memory graph."""

import asyncio

from cognee_community_connector_hydradb import hydradb_source


async def main() -> None:
    print("Initializing HydraDB connector source...")
    # Configure connector with database/collection filters
    source = hydradb_source(
        api_key="your_hydradb_api_key",
        database_id="enterprise_knowledge",
        collection_id="engineering_handbook",
        category="knowledge",
    )

    print(f"Created dlt source: {source.name}")
    print(f"Available resources: {list(source.resources.keys())}")


if __name__ == "__main__":
    asyncio.run(main())
