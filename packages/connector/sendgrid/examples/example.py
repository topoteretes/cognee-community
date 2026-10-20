"""SendGrid connector demo — ingest email templates and marketing lists into AI agent memory.

Pulls SendGrid dynamic templates and marketing contact lists into cognee with
forget-on-delete semantics. ``sendgrid_source`` returns a ``dlt`` source that you
pass directly to ``cognee.remember`` — no manual routing needed.

Documents are ingested as normal prose documents (flowing through cognee's cognify
entity-extraction pipeline) via cognee's document-mode marker.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Generate a SendGrid API Key:
   Log in to SendGrid -> Settings -> API Keys -> Create Key (Read Access to Templates & Marketing).
2. Export your keys:

       export SENDGRID_API_KEY="SG.your_api_key_here"
       export LLM_API_KEY="sk-..."
       python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_sendgrid import sendgrid_source

# Maintain SendGrid in its own dedicated dataset for easy inspection and pruning
DATASET_NAME = "sendgrid"


async def main() -> None:
    if not os.environ.get("SENDGRID_API_KEY"):
        print("Set SENDGRID_API_KEY to run this example.")
        print("Example: export SENDGRID_API_KEY='SG.your_api_key'")
        return

    # Ingest SendGrid templates and marketing lists
    source = sendgrid_source()

    print("Syncing SendGrid email templates and lists into Cognee memory ...")
    await cognee.remember(source, dataset_name=DATASET_NAME, self_improvement=False)

    print("\nQuerying knowledge graph ...")
    answer = await cognee.search(
        query_text="What email templates do we have for password resets or onboarding, and what are their subject lines?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)


if __name__ == "__main__":
    asyncio.run(main())
