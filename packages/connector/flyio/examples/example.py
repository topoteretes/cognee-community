"""Fly.io connector demo — "Ask my global infrastructure & machines".

Pull your Fly.io apps, machines, regions, and volumes into cognee memory,
incrementally, with automated token redaction and forget-on-delete.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the connector package:

       pip install cognee-community-connector-flyio

2. Generate an API token using the Fly CLI or dashboard:

       fly auth token

3. Export it:

       export FLY_API_TOKEN="FlyV1 ..."

4. Set your LLM API key in your environment.

Run it:

    python examples/example.py
"""

import asyncio
import os

import cognee
from cognee_community_connector_flyio import flyio_source

DATASET_NAME = "infrastructure_memory"
RESOURCE_NAME = "flyio_prod"

FLYIO_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
}


async def sync(token: str) -> None:
    source = flyio_source(api_key=token, resource_name=RESOURCE_NAME)
    result = await cognee.remember(source, dataset_name=DATASET_NAME, **FLYIO_REMEMBER_KWARGS)
    print("Sync result:", result)
    print("Sync stats:", source.cognee_sync_stats)


async def main():
    token = os.environ.get("FLY_API_TOKEN")
    if not token:
        print("Set FLY_API_TOKEN.\nSee the setup steps in this file's docstring, then re-run.")
        return

    print("\n=== Fly.io Sync #1 (Backfill) ===")
    await sync(token)

    answer = await cognee.search(
        query_text="What apps are currently deployed, and in which regions are machines running?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nInfrastructure Copilot Answer:\n", answer)

    print("\n=== Fly.io Sync #2 (Incremental) ===")
    await sync(token)


if __name__ == "__main__":
    asyncio.run(main())
