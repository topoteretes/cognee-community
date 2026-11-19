"""End-to-end example: Ingesting AsyncAPI specifications into Cognee.

Prerequisites:
    pip install cognee-community-connector-asyncapi cognee

Usage:
    export ASYNCAPI_PATH="/path/to/asyncapi.yaml"
    python example.py
"""

import asyncio
import os
from pathlib import Path

import cognee

from cognee_community_connector_asyncapi import asyncapi_source


async def main():
    spec_path = os.environ.get("ASYNCAPI_PATH", "asyncapi.yaml")
    print(f"Connecting to AsyncAPI specification at: {Path(spec_path).resolve()}")

    # 1. Initialize AsyncAPI DLT source
    source = asyncapi_source(spec_path=spec_path)

    # 2. Add event channels and message schemas into Cognee memory
    print("Ingesting AsyncAPI channels into Cognee memory...")
    await cognee.add(source)

    # 3. Build knowledge graph
    print("Cognifying event-driven architecture into knowledge graph...")
    await cognee.cognify()
    print("Ingestion and graph cognification complete!")

    # 4. Search and retrieve insights from the persistent graph memory
    query = "What Kafka topics receive user registration events?"
    print(f"\nQuerying knowledge graph: '{query}'")
    results = await cognee.search(
        query_type="INSIGHTS",
        query_text=query,
    )
    print("\n=== Cognee Knowledge Graph Insights ===")
    print(results)


if __name__ == "__main__":
    asyncio.run(main())
