"""Unkey API key infrastructure connector demo — "ask my API key stack".

Pull your Unkey APIs, key metadata, identities, and rate limit policies
into cognee memory, incrementally, with forget-on-delete and zero secret leakage.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install dependencies:

       pip install cognee-community-connector-unkey

2. Set your Unkey Root Key:

       export UNKEY_ROOT_KEY="unkey_test_token"

3. Set your LLM API key (e.g. ``LLM_API_KEY``) in your environment.

Run it:

    python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_unkey import unkey_source

DATASET_NAME = "unkey_infrastructure"
RESOURCE_NAME = "unkey_main_workspace"

UNKEY_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
}


async def sync(root_key: str) -> None:
    source = unkey_source(
        root_key=root_key,
        resource_name=RESOURCE_NAME,
        include=("apis", "keys", "identities"),
    )
    result = await cognee.remember(source, dataset_name=DATASET_NAME, **UNKEY_REMEMBER_KWARGS)
    print("Ingestion result:", result)
    print("Sync stats:", source.cognee_sync_stats)


async def main():
    root_key = os.environ.get("UNKEY_ROOT_KEY")

    if not root_key:
        print("Set UNKEY_ROOT_KEY.\nSee the setup steps in this file's docstring, then re-run.")
        return

    print("\n=== Unkey sync #1 (backfill) ===")
    await sync(root_key)

    query = (
        "Which API keys belong to tenant cust_acme, "
        "and what rate limits and remaining quotas are configured?"
    )
    answer = await cognee.search(
        query_text=query,
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nUnkey answer:\n", answer)

    print("\n=== Unkey sync #2 (incremental update) ===")
    await sync(root_key)


if __name__ == "__main__":
    asyncio.run(main())
