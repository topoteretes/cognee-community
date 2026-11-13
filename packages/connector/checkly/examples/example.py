"""Checkly synthetic monitoring connector demo — "ask my monitoring stack".

Pull your Checkly synthetic API and browser checks, check groups, alert channels,
and maintenance windows into cognee memory, incrementally, with forget-on-delete
and zero secret header or environment credential leakage.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install dependencies:

       pip install cognee-community-connector-checkly

2. Set your Checkly API key (User Settings -> API Keys):

       export CHECKLY_API_KEY="cu_..."
       # Optional: export CHECKLY_ACCOUNT_ID="acc_..." (for multi-account teams)

3. Set your LLM API key (e.g. ``LLM_API_KEY``) in your environment.

Run it:

    python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_checkly import checkly_source

DATASET_NAME = "checkly_synthetic_monitoring"
RESOURCE_NAME = "checkly_production_workspace"

CHECKLY_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
}


async def sync(api_key: str, account_id: str | None = None) -> None:
    source = checkly_source(
        api_key=api_key,
        account_id=account_id,
        resource_name=RESOURCE_NAME,
        include=("checks", "check_groups", "alert_channels", "maintenance_windows"),
    )
    result = await cognee.remember(source, dataset_name=DATASET_NAME, **CHECKLY_REMEMBER_KWARGS)
    print("Ingestion result:", result)
    print("Sync stats:", source.cognee_sync_stats)


async def main():
    api_key = os.environ.get("CHECKLY_API_KEY")
    account_id = os.environ.get("CHECKLY_ACCOUNT_ID")

    if not api_key:
        print("Set CHECKLY_API_KEY.\nSee the setup steps in this file's docstring, then re-run.")
        return

    print("\n=== Checkly sync #1 (backfill) ===")
    await sync(api_key, account_id)

    query = (
        "Which synthetic API checks are failing or degraded, "
        "and which alert channels notify on failure?"
    )
    answer = await cognee.search(
        query_text=query,
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nMonitoring answer:", answer)

    print("\n=== Checkly sync #2 (incremental update) ===")
    await sync(api_key, account_id)


if __name__ == "__main__":
    asyncio.run(main())
