"""UptimeRobot uptime monitoring connector demo — "ask my uptime stack".

Pull your UptimeRobot monitors, public status pages, alert contacts,
and maintenance windows into cognee memory, incrementally, with forget-on-delete
and zero credential leakage.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install dependencies:

       pip install cognee-community-connector-uptimerobot

2. Set your UptimeRobot API key (My Settings -> API Settings):

       export UPTIMEROBOT_API_KEY="u123456-..."

3. Set your LLM API key (e.g. ``LLM_API_KEY``) in your environment.

Run it:

    python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_uptimerobot import uptimerobot_source

DATASET_NAME = "uptimerobot_monitoring"
RESOURCE_NAME = "uptimerobot_main_workspace"

UPTIMEROBOT_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
}


async def sync(api_key: str) -> None:
    source = uptimerobot_source(
        api_key=api_key,
        resource_name=RESOURCE_NAME,
        include=("monitors", "public_status_pages", "alert_contacts", "maintenance_windows"),
    )
    result = await cognee.remember(source, dataset_name=DATASET_NAME, **UPTIMEROBOT_REMEMBER_KWARGS)
    print("Ingestion result:", result)
    print("Sync stats:", source.cognee_sync_stats)


async def main():
    api_key = os.environ.get("UPTIMEROBOT_API_KEY")

    if not api_key:
        print(
            "Set UPTIMEROBOT_API_KEY.\nSee the setup steps in this file's docstring, then re-run."
        )
        return

    print("\n=== UptimeRobot sync #1 (backfill) ===")
    await sync(api_key)

    query = (
        "Which production monitors are DOWN or degraded, "
        "and what are their response times and alert contacts?"
    )
    answer = await cognee.search(
        query_text=query,
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nUptime answer:\n", answer)

    print("\n=== UptimeRobot sync #2 (incremental update) ===")
    await sync(api_key)


if __name__ == "__main__":
    asyncio.run(main())
