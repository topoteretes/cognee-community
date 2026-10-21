"""Trigger.dev connector demo — "Ask my background jobs & task executions".

Pull your Trigger.dev tasks, runs, error stacktraces, and cron schedules into cognee memory,
incrementally, with forget-on-delete.

This example is built on cognee's DLT ingestion subsystem: ``triggerdev_source``
returns a ``dlt`` resource that you hand straight to ``cognee.remember``. The
first run backfills the project history; subsequent runs re-process only
records whose content changed, and deleted tasks or schedules are forgotten
from memory on the next sync.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the connector package:

       pip install cognee-community-connector-triggerdev

2. Obtain a Trigger.dev Project API Key from Trigger.dev Dashboard → Project Settings → API Keys.

3. Export it:

       export TRIGGERDEV_API_KEY="tr_prod_..."

4. Set your LLM API key (e.g. ``OPENAI_API_KEY`` or ``ANTHROPIC_API_KEY``) in your environment.

Run it:

    python examples/example.py
"""

import asyncio
import os

import cognee
from cognee_community_connector_triggerdev import triggerdev_source

DATASET_NAME = "triggerdev_ops"
RESOURCE_NAME = "triggerdev_main"

TRIGGERDEV_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
}


async def sync(api_key: str) -> None:
    source = triggerdev_source(api_key=api_key, resource_name=RESOURCE_NAME)
    result = await cognee.remember(source, dataset_name=DATASET_NAME, **TRIGGERDEV_REMEMBER_KWARGS)
    print("Sync result:", result)
    print("Sync stats:", source.cognee_sync_stats)


async def main():
    api_key = os.environ.get("TRIGGERDEV_API_KEY")
    if not api_key:
        print("Set TRIGGERDEV_API_KEY.\nSee the setup steps in this file's docstring, then re-run.")
        return

    print("\n=== Trigger.dev Sync #1 (Backfill) ===")
    await sync(api_key)

    answer = await cognee.search(
        query_text="Which background tasks failed in the last runs, and what were the error messages?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nOps Assistant Answer:\n", answer)

    print("\n=== Trigger.dev Sync #2 (Incremental) ===")
    await sync(api_key)


if __name__ == "__main__":
    asyncio.run(main())
