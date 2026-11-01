"""Example usage of the Cognee Databricks Unity Catalog connector."""

import asyncio
import os

import cognee

from cognee_community_connector_unitycatalog import unitycatalog_source


async def main():
    workspace_url = os.environ.get("DATABRICKS_HOST", "https://adb-12345.azuredatabricks.net")
    token = os.environ.get("DATABRICKS_TOKEN", "mock-token")

    # Ingest lakehouse catalogs, schemas, tables with column types, and AI models
    source = unitycatalog_source(
        base_url=workspace_url,
        token=token,
        catalogs=["production", "analytics"],
        include_tables=True,
        include_models=True,
    )

    print("Adding Databricks Unity Catalog assets to Cognee...")
    await cognee.add(source)

    print("Running Cognify on lakehouse metadata...")
    await cognee.cognify()

    print("Searching for schema definitions and AI models...")
    results = await cognee.search("Which Delta table in the analytics catalog stores fraud scores?")
    for res in results:
        print("-", res)


if __name__ == "__main__":
    asyncio.run(main())
