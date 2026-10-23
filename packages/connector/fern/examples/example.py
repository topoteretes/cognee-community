"""End-to-end example: Ingesting Fern API definitions into Cognee.

Prerequisites:
    pip install cognee-community-connector-fern cognee

Usage:
    export FERN_PATH="/path/to/your/fern/repo"
    python example.py
"""

import asyncio
import os
from pathlib import Path

import cognee

from cognee_community_connector_fern import fern_source


async def main():
    fern_dir = os.environ.get("FERN_PATH", "./fern")
    print(f"Connecting to Fern API definition at: {Path(fern_dir).resolve()}")

    # 1. Initialize Fern DLT source
    source = fern_source(fern_path=fern_dir)

    # 2. Ingest API endpoints and guides into Cognee
    print("Ingesting Fern API definitions into Cognee memory...")
    await cognee.add(source)

    # 3. Build knowledge graph
    print("Cognifying API schemas into knowledge graph...")
    await cognee.cognify()
    print("Ingestion and graph cognification complete!")

    # 4. Search and retrieve insights from the persistent graph memory
    query = "What endpoints are available for user profile management?"
    print(f"\nQuerying knowledge graph: '{query}'")
    results = await cognee.search(
        query_type="INSIGHTS",
        query_text=query,
    )
    print("\n=== Cognee Knowledge Graph Insights ===")
    print(results)


if __name__ == "__main__":
    asyncio.run(main())
