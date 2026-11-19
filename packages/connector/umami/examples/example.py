"""Umami connector demo — ingest privacy-friendly website analytics into AI agent memory.

Pulls Umami tracked websites, visitor totals, pageviews, and bounce statistics into cognee
with forget-on-delete semantics. ``umami_source`` returns a ``dlt`` source that you pass
directly to ``cognee.remember`` — no manual routing needed.

Documents are ingested as normal prose documents (flowing through cognee's cognify
entity-extraction pipeline) via cognee's document-mode marker.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Obtain your Umami API Key:
   Log in to Umami -> Settings -> API Keys -> Create API Key.
2. Export your keys:

       export UMAMI_API_KEY="your_api_key_here"
       export UMAMI_BASE_URL="https://api.umami.is/v1"  # Optional, default cloud endpoint
       export LLM_API_KEY="sk-..."
       python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_umami import umami_source

# Maintain Umami in its own dedicated dataset for easy inspection and lifecycle management
DATASET_NAME = "umami"


async def main() -> None:
    if not os.environ.get("UMAMI_API_KEY"):
        print("Set UMAMI_API_KEY to run this example.")
        print("Example: export UMAMI_API_KEY='your_api_key'")
        return

    # Ingest Umami website traffic reports
    source = umami_source()

    print("Syncing Umami website analytics into Cognee memory ...")
    await cognee.remember(source, dataset_name=DATASET_NAME, self_improvement=False)

    print("\nQuerying knowledge graph ...")
    answer = await cognee.search(
        query_text="What are our top visited website pages and what is the bounce rate trend?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)


if __name__ == "__main__":
    asyncio.run(main())
