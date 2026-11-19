"""Example demonstrating Netdata infrastructure monitoring & alarm ingestion into Cognee.

This script fetches system topology, CPU/RAM capacity, active health alarms,
and metric chart definitions from a Netdata node, indexes them into Cognee memory via cognify(),
and queries infrastructure diagnostics and active alerts.
"""

from __future__ import annotations

import asyncio
import os

import cognee

from cognee_community_connector_netdata import (
    NetdataConfig,
    netdata_connector,
)

DATASET_NAME = "infrastructure_telemetry"


async def main() -> None:
    # 1. Initialize connector configuration
    config = NetdataConfig(
        base_url=os.getenv("NETDATA_BASE_URL", "http://localhost:19999"),
        api_token=os.getenv("NETDATA_API_TOKEN", None),
        include_alarms=True,
        include_charts=True,
        alarm_status_filter=["CRITICAL", "WARNING"],
    )

    print("Fetching Netdata system topology, active alarms, and metric catalogs...")
    documents = await netdata_connector(config)
    print(f"Retrieved {len(documents)} structured infrastructure documents.")

    # 2. Ingest documents into Cognee dataset
    print(f"\nAdding documents to Cognee dataset '{DATASET_NAME}'...")
    await cognee.add(documents, dataset_name=DATASET_NAME)

    # 3. Cognify into knowledge graph and vector indices
    print("Cognifying dataset to build infrastructure & incident graph...")
    await cognee.cognify(dataset_name=DATASET_NAME)

    # 4. Search and reason over infrastructure memory
    queries = [
        "Which server nodes are currently raising active CRITICAL or WARNING alarms?",
        "What is the CPU core count, RAM capacity, and kernel architecture of the host?",
        "Show me all health alarms related to disk space utilization or memory exhaustion.",
    ]

    for query in queries:
        print(f"\nQuery: '{query}'")
        results = await cognee.search(query, dataset_name=DATASET_NAME)
        for res in results:
            print(f"- {res}")


if __name__ == "__main__":
    asyncio.run(main())
