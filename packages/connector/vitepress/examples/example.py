"""End-to-end example: Ingesting VitePress documentation into Cognee.

Prerequisites:
    pip install cognee-community-connector-vitepress cognee

Usage:
    export VITEPRESS_PATH="/path/to/your/vitepress/site"
    python example.py
"""

import asyncio
import os
from pathlib import Path

import cognee

from cognee_community_connector_vitepress import vitepress_source


async def main():
    site_dir = os.environ.get("VITEPRESS_PATH", ".")
    print(f"Connecting to VitePress site at: {Path(site_dir).resolve()}")

    # 1. Initialize VitePress DLT source
    source = vitepress_source(site_path=site_dir)

    # 2. Ingest documentation pages into Cognee memory
    print("Ingesting VitePress pages into Cognee memory...")
    await cognee.add(source)

    # 3. Build knowledge graph
    print("Cognifying documents into knowledge graph...")
    await cognee.cognify()
    print("Ingestion and graph cognification complete!")

    # 4. Search and retrieve insights from the persistent graph memory
    query = "What are the core architecture concepts and configuration rules?"
    print(f"\nQuerying knowledge graph: '{query}'")
    results = await cognee.search(
        query_type="INSIGHTS",
        query_text=query,
    )
    print("\n=== Cognee Knowledge Graph Insights ===")
    print(results)


if __name__ == "__main__":
    asyncio.run(main())
