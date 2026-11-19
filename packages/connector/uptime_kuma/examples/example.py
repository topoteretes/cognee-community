"""Uptime Kuma connector example — turn infrastructure health into AI memory.

Syncs server monitors, latency health checks, downtime incidents, and status-page
announcements into Cognee's persistent knowledge graph using the Uptime Kuma connector.

────────────────────────────────────────────────────────────────────────────
Quickstart:
────────────────────────────────────────────────────────────────────────────
1. Provide your Uptime Kuma base URL (e.g. self-hosted server or public status page):

       export UPTIME_KUMA_BASE_URL="http://localhost:3001"
       export UPTIME_KUMA_API_KEY="optional_api_key"
       export LLM_API_KEY="sk-..."

2. Run this script:

       uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_uptime_kuma import uptime_kuma_source

DATASET_NAME = "infrastructure_monitoring"


async def main() -> None:
    base_url = os.environ.get("UPTIME_KUMA_BASE_URL", "http://localhost:3001")
    api_key = os.environ.get("UPTIME_KUMA_API_KEY")
    slug = os.environ.get("UPTIME_KUMA_SLUG", "default")

    print(f"Connecting to Uptime Kuma at {base_url} (status slug: '{slug}') ...")

    # Instantiate the dlt source
    source = uptime_kuma_source(
        base_url=base_url,
        api_key=api_key,
        slug=slug,
        include_heartbeats=True,
        include_incidents=True,
    )

    print(f"Ingesting Uptime Kuma telemetry into Cognee dataset '{DATASET_NAME}' ...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    print("\nRunning test recall query on infrastructure memory ...")
    query = "Which services experienced downtime recently and what were the error messages?"
    answer = await cognee.search(
        query_text=query,
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )

    print(f"\nQuestion: {query}")
    print(f"Answer:\n{answer}\n")
    print("Done! Infrastructure monitoring is now integrated into AI agent memory.")


if __name__ == "__main__":
    asyncio.run(main())
