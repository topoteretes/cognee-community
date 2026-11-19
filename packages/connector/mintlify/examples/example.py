"""End-to-end example: Ingesting Mintlify documentation into Cognee.

Prerequisites:
    pip install cognee-community-connector-mintlify cognee

Usage:
    export MINTLIFY_DOCS_PATH="/path/to/your/mintlify/repo"
    python example.py
"""

import asyncio
import os
from pathlib import Path

import cognee

from cognee_community_connector_mintlify import mintlify_source


async def main():
    docs_dir = os.environ.get("MINTLIFY_DOCS_PATH", ".")
    print(f"Connecting to Mintlify documentation at: {Path(docs_dir).resolve()}")

    # 1. Initialize Mintlify DLT source
    source = mintlify_source(docs_path=docs_dir)

    # 2. Add documentation pages to Cognee memory pipeline
    print("Ingesting documentation pages into Cognee memory...")
    await cognee.add(source)

    # 3. Build the knowledge graph (entity extraction and graph relations)
    print("Cognifying documents into knowledge graph...")
    await cognee.cognify()
    print("Ingestion and graph cognification complete!")

    # 4. Search and retrieve insights from the persistent graph memory
    query = "What endpoints and authentication headers does the API require?"
    print(f"\nQuerying knowledge graph: '{query}'")
    results = await cognee.search(
        query_type="INSIGHTS",
        query_text=query,
    )
    print("\n=== Cognee Knowledge Graph Insights ===")
    print(results)


if __name__ == "__main__":
    asyncio.run(main())
