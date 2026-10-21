"""Example demonstrating Apache Iceberg lakehouse ingestion into cognee.

Requires:
    pip install "cognee-community-connector-iceberg"
"""

import asyncio
import os

import cognee

from cognee_community_connector_iceberg import iceberg_source

DATASET_NAME = "lakehouse_metadata"


async def main():
    # Configure connection to your Iceberg Catalog (e.g. REST Catalog, AWS Glue, Hive)
    catalog_properties = {
        "type": os.environ.get("ICEBERG_CATALOG_TYPE", "rest"),
        "uri": os.environ.get("ICEBERG_CATALOG_URI", "http://localhost:8181"),
        "warehouse": os.environ.get("ICEBERG_WAREHOUSE", "s3://my-lakehouse-bucket/warehouse"),
        "token": os.environ.get("ICEBERG_REST_TOKEN", "my-bearer-token"),
    }

    # Initialize the dlt source for specific namespaces, or omit to discover all
    source = iceberg_source(
        catalog_properties=catalog_properties,
        namespaces=[("analytics",), ("finance",)],
        include_snapshots=True,
    )

    print("Syncing Apache Iceberg table schemas and metadata into cognee memory...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    query = "Which tables in finance are partitioned by day and what are their schema fields?"
    answer = await cognee.search(
        query_text=query,
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nGraph Search Answer:\n", answer)


if __name__ == "__main__":
    asyncio.run(main())
