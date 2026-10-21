"""End-to-end example: Ingesting Docusaurus documentation into Cognee.

Prerequisites:
    pip install cognee-community-connector-docusaurus cognee

Usage:
    export DOCUSAURUS_PATH="/path/to/your/docusaurus/site"
    python example.py
"""

import asyncio
import os
from pathlib import Path

import cognee

from cognee_community_connector_docusaurus import docusaurus_source


async def main():
    site_dir = os.environ.get("DOCUSAURUS_PATH", ".")
    print(f"Connecting to Docusaurus site at: {Path(site_dir).resolve()}")

    # 1. Initialize Docusaurus DLT source
    source = docusaurus_source(site_path=site_dir)

    # 2. Add documentation pages to Cognee memory pipeline
    print("Ingesting Docusaurus documentation into Cognee memory...")
    await cognee.add(source)

    # 3. Build knowledge graph
    print("Cognifying documents into knowledge graph...")
    await cognee.cognify()
    print("Ingestion and graph cognification complete!")

    # 4. Search and retrieve insights from the persistent graph memory
    query = "What are the core platform concepts and setup requirements?"
    print(f"\nQuerying knowledge graph: '{query}'")
    results = await cognee.search(
        query_type="INSIGHTS",
        query_text=query,
    )
    print("\n=== Cognee Knowledge Graph Insights ===")
    print(results)


if __name__ == "__main__":
    asyncio.run(main())
