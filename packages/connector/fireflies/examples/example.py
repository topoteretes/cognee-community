"""Fireflies.ai connector demo — turn meeting conversations into AI agent memory.

Pulls Fireflies meeting transcripts, summaries, action items, and speaker-attributed
dialogue into cognee. Ingested documents flow through cognee's knowledge graph extraction
pipeline, enabling agents to remember who said what and who committed to which action item.

────────────────────────────────────────────────────────────────────────────
Setup
────────────────────────────────────────────────────────────────────────────
1. Install requirements:
       pip install cognee-community-connector-fireflies

2. Obtain a Fireflies API key from https://app.fireflies.ai/integrations/api

3. Set your environment variables:
       export FIREFLIES_API_KEY="your_fireflies_api_key"
       export LLM_API_KEY="your_llm_api_key"  # e.g., OpenAI or Anthropic

4. Run the script:
       python examples/example.py
"""

import asyncio
import os

import cognee
from cognee_community_connector_fireflies import fireflies_source

DATASET_NAME = "fireflies_meeting_memory"


async def main() -> None:
    api_key = os.environ.get("FIREFLIES_API_KEY")
    if not api_key:
        print("Please set the FIREFLIES_API_KEY environment variable to run this demo.")
        return

    print("Connecting to Fireflies.ai and configuring DLT ingestion...")
    # Fetch meetings from the past 30 days
    source = fireflies_source(
        api_key=api_key,
        days_back=30,
        include_action_items=True,
        include_sentences=True,
    )

    print("Ingesting meeting transcripts into cognee knowledge graph...")
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",
    )
    print("Ingestion complete!")

    print("\nQuerying agent memory across recorded meetings...")
    query = "What action items and commitments were made in recent team syncs?"
    result = await cognee.search(
        query_text=query,
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )

    print("\n" + "=" * 60)
    print("AGENT RECALL RESULT:")
    print("=" * 60)
    print(result)
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
