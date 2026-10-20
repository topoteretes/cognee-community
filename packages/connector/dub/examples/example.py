"""Dub.co connector demo — ingest short links and marketing campaign metadata into AI agent memory.

Pulls Dub.co links and click engagement metrics into cognee with forget-on-delete
semantics. ``dub_source`` returns a ``dlt`` source that you pass directly to
``cognee.remember`` — no manual routing needed.

Documents are ingested as normal prose documents (flowing through cognee's cognify
entity-extraction pipeline) via cognee's document-mode marker.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Obtain your Dub.co API Key:
   Log in to Dub.co -> Workspace Settings -> API Keys -> Create API Key.
2. Export your keys:

       export DUB_API_KEY="dub_..."
       export LLM_API_KEY="sk-..."
       python examples/example.py
"""

import asyncio
import os

import cognee
from cognee_community_connector_dub import dub_source

# Maintain Dub.co in its own dedicated dataset for easy inspection and lifecycle management
DATASET_NAME = "dub"


async def main() -> None:
    if not os.environ.get("DUB_API_KEY"):
        print("Set DUB_API_KEY to run this example.")
        print("Example: export DUB_API_KEY='dub_your_token_here'")
        return

    # Ingest Dub.co short links
    source = dub_source()

    print("Syncing Dub.co campaign links into Cognee memory ...")
    await cognee.remember(source, dataset_name=DATASET_NAME, self_improvement=False)

    print("\nQuerying knowledge graph ...")
    answer = await cognee.search(
        query_text="What campaign short links do we have, where do they redirect, and which ones have the highest clicks?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)


if __name__ == "__main__":
    asyncio.run(main())
