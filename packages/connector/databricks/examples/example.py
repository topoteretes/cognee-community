"""Example script demonstrating how to ingest Databricks assets into cognee."""

import asyncio
import os

import cognee

from cognee_community_connector_databricks import databricks_source


async def main() -> None:
    host = os.getenv("DATABRICKS_HOST", "https://dbc-12345678-abcd.cloud.databricks.com")
    token = os.getenv("DATABRICKS_TOKEN", "dapi_mock_token")
    warehouse_id = os.getenv("DATABRICKS_WAREHOUSE_ID")

    # 1. Define explicit queries (optional)
    queries = []
    if warehouse_id:
        queries.append(
            {
                "name": "active_users",
                "statement": (
                    "SELECT user_id, email, count(*) as event_count "
                    "FROM events GROUP BY 1, 2 LIMIT 100"
                ),
                "warehouse_id": warehouse_id,
                "primary_key": "user_id",
            }
        )

    # 2. Build the Databricks dlt source across notebooks, tables, and queries
    source = databricks_source(
        host=host,
        token=token,
        include=["notebooks", "tables", "queries"] if queries else ["notebooks", "tables"],
        workspace_paths=["/Shared"],
        catalogs=["main"],
        warehouse_id=warehouse_id,
        queries=queries,
        write_disposition="merge",
    )

    # 3. Ingest into cognee memory
    print("Ingesting Databricks workspace assets into cognee memory...")
    await cognee.remember(
        source,
        dataset_name="databricks_knowledge",
    )
    print("Ingestion complete! Searching cognee memory...")

    # 4. Search ingested memory
    results = await cognee.search(
        query_text="Find data engineering pipelines and customer schemas in Databricks",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=["databricks_knowledge"],
    )
    print("Search Results:\n", results)


if __name__ == "__main__":
    asyncio.run(main())
