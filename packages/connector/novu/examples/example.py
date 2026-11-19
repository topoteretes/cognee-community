"""Novu notification infrastructure connector demo — "ask my notification stack".

Pull your Novu workflows, topics, notification groups, and integration providers
into cognee memory, incrementally, with forget-on-delete and zero credential leakage.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install dependencies:

       pip install cognee-community-connector-novu

2. Set your Novu API key:

       export NOVU_API_KEY="test_novu_api_key"

3. Set your LLM API key (e.g. ``LLM_API_KEY``) in your environment.

Run it:

    python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_novu import novu_source

DATASET_NAME = "novu_notifications"
RESOURCE_NAME = "novu_main_workspace"

NOVU_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
}


async def sync(api_key: str) -> None:
    source = novu_source(
        api_key=api_key,
        resource_name=RESOURCE_NAME,
        include=("workflows", "topics", "notification_groups", "integrations"),
    )
    result = await cognee.remember(source, dataset_name=DATASET_NAME, **NOVU_REMEMBER_KWARGS)
    print("Ingestion result:", result)
    print("Sync stats:", source.cognee_sync_stats)


async def main():
    api_key = os.environ.get("NOVU_API_KEY")

    if not api_key:
        print("Set NOVU_API_KEY.\nSee the setup steps in this file's docstring, then re-run.")
        return

    print("\n=== Novu sync #1 (backfill) ===")
    await sync(api_key)

    query = (
        "Which notification workflows send SMS or Slack alerts, "
        "and what third-party providers are configured?"
    )
    answer = await cognee.search(
        query_text=query,
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nNotification answer:\n", answer)

    print("\n=== Novu sync #2 (incremental update) ===")
    await sync(api_key)


if __name__ == "__main__":
    asyncio.run(main())
