"""New Relic observability connector demo.

Ingests alert policies, NRQL conditions, dashboards, and incident events
from New Relic into Cognee memory with incremental updates and forget-on-delete.

Prerequisites:
  1. Generate a New Relic User API Key:
     New Relic One -> User profile -> API keys -> Create key (User key).
  2. Set environment variables:
     export NEW_RELIC_API_KEY="NRAK-..."
     export NEW_RELIC_ACCOUNT_ID="1234567"
     export LLM_API_KEY="sk-..."
  3. Run:
     uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_new_relic import new_relic_source

DATASET_NAME = "new_relic_telemetry"


async def main() -> None:
    api_key = os.environ.get("NEW_RELIC_API_KEY")
    account_id = os.environ.get("NEW_RELIC_ACCOUNT_ID")

    if not api_key or not account_id:
        print("Please export NEW_RELIC_API_KEY and NEW_RELIC_ACCOUNT_ID to run this demo.")
        return

    print("Connecting to New Relic NerdGraph and syncing observability documents...")
    source = new_relic_source(api_key=api_key, account_id=account_id)

    # Ingest observability definitions and active incident history
    await cognee.remember(source, dataset_name=DATASET_NAME)
    print("Ingestion complete!")

    # Query operational context
    query = "What alert policies are currently configured and what incidents have fired?"
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
