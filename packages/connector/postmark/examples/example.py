"""Postmark connector demo — ingest transactional templates into AI agent memory.

Pulls Postmark email templates into cognee with forget-on-delete semantics.
``postmark_source`` returns a ``dlt`` source that you pass directly to
``cognee.remember`` — no manual routing needed.

Documents are ingested as normal prose documents (flowing through cognee's cognify
entity-extraction pipeline) via cognee's document-mode marker.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Obtain your Postmark Server API Token:
   Log in to Postmark -> Select Server -> API Tokens -> Copy Server API Token.
2. Export your keys:

       export POSTMARK_SERVER_TOKEN="your_server_token_here"
       export LLM_API_KEY="sk-..."
       python examples/example.py
"""

import asyncio
import os

import cognee
from cognee_community_connector_postmark import postmark_source

# Maintain Postmark in its own dedicated dataset for easy inspection and lifecycle management
DATASET_NAME = "postmark"


async def main() -> None:
    if not os.environ.get("POSTMARK_SERVER_TOKEN"):
        print("Set POSTMARK_SERVER_TOKEN to run this example.")
        print("Example: export POSTMARK_SERVER_TOKEN='your_server_token_here'")
        return

    # Ingest Postmark transactional templates
    source = postmark_source()

    print("Syncing Postmark email templates into Cognee memory ...")
    await cognee.remember(source, dataset_name=DATASET_NAME, self_improvement=False)

    print("\nQuerying knowledge graph ...")
    answer = await cognee.search(
        query_text="What transactional email templates do we have for receipts and invoices?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)


if __name__ == "__main__":
    asyncio.run(main())
