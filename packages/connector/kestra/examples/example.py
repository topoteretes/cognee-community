"""Example demonstrating how to ingest Kestra orchestration telemetry into Cognee."""

import asyncio
import os
import cognee
from cognee_community_connector_kestra import kestra_source


async def main() -> None:
    # 1. Initialize Kestra connector
    source = kestra_source(
        base_url=os.getenv("KESTRA_BASE_URL", "http://localhost:8080"),
        token=os.getenv("KESTRA_TOKEN"),
        namespaces=["prod", "etl"],
        include_logs=True,
    )

    # 2. Add to Cognee memory
    print("Ingesting Kestra flows and failure logs into Cognee...")
    await cognee.add(source)

    # 3. Cognify into knowledge graph
    print("Running Cognify extraction pipeline...")
    await cognee.cognify()

    # 4. Search DevOps operational intelligence
    query = "Why did the nightly ingest flow fail last week, and what error log was reported?"
    print(f"\nSearching Cognee memory: '{query}'")
    results = await cognee.search(query)
    for result in results:
        print(f"- {result}")


if __name__ == "__main__":
    asyncio.run(main())
