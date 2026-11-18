"""Slab knowledge base connector demo.

Ingests team wiki posts, topics, and comments into Cognee memory with
incremental updates and forget-on-delete tombstoning.

Prerequisites:
  1. Generate a Slab API token: Slab Settings -> Integrations -> Developer API -> Create Token.
  2. Set environment variables:
     export SLAB_API_TOKEN="your_token_here"
     export LLM_API_KEY="sk-..."
  3. Run:
     uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_slab import slab_source

DATASET_NAME = "slab_docs"


async def main() -> None:
    token = os.environ.get("SLAB_API_TOKEN")
    if not token:
        print("Please export SLAB_API_TOKEN to run this example.")
        return

    print("Connecting to Slab and synchronizing posts into Cognee memory...")
    source = slab_source(api_token=token)

    # Ingest posts and extract knowledge graph
    await cognee.remember(source, dataset_name=DATASET_NAME)
    print("Ingestion complete!")

    # Query the ingested knowledge base
    query = "What engineering guidelines or architecture decisions are documented in Slab?"
    print(f"\nQuerying knowledge graph: '{query}'")
    results = await cognee.search(
        query_text=query,
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch Result:")
    print(results)


if __name__ == "__main__":
    asyncio.run(main())
