"""Svix webhooks and event infrastructure connector demo — "ask my webhook stack".

Pull your Svix applications, endpoint subscriptions, and event type schemas
into cognee memory, incrementally, with forget-on-delete and zero secret leakage.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install dependencies:

       pip install cognee-community-connector-svix

2. Set your Svix API token:

       export SVIX_API_TOKEN="test_svix_token"

3. Set your LLM API key (e.g. ``LLM_API_KEY``) in your environment.

Run it:

    python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_svix import svix_source

DATASET_NAME = "svix_webhooks"
RESOURCE_NAME = "svix_main_workspace"

SVIX_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
}


async def sync(api_token: str) -> None:
    source = svix_source(
        api_token=api_token,
        resource_name=RESOURCE_NAME,
        include=("applications", "endpoints", "event_types"),
    )
    result = await cognee.remember(source, dataset_name=DATASET_NAME, **SVIX_REMEMBER_KWARGS)
    print("Ingestion result:", result)
    print("Sync stats:", source.cognee_sync_stats)


async def main():
    api_token = os.environ.get("SVIX_API_TOKEN")

    if not api_token:
        print("Set SVIX_API_TOKEN.\nSee the setup steps in this file's docstring, then re-run.")
        return

    print("\n=== Svix sync #1 (backfill) ===")
    await sync(api_token)

    query = (
        "Which webhook endpoints are subscribed to user.created events, "
        "and are any endpoints currently disabled?"
    )
    answer = await cognee.search(
        query_text=query,
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nWebhook answer:\n", answer)

    print("\n=== Svix sync #2 (incremental update) ===")
    await sync(api_token)


if __name__ == "__main__":
    asyncio.run(main())
