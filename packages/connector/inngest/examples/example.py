"""Inngest connector demo — "Ask my background workflows & execution traces".

Pull your Inngest durable functions, execution runs, error stacktraces, and event triggers into cognee memory,
incrementally, with automated secret redaction and forget-on-delete.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the connector package:

       pip install cognee-community-connector-inngest

2. Generate or copy your Inngest Signing / Management Key:
   Inngest Dashboard → Settings → Keys.

3. Export it:

       export INNGEST_SIGNING_KEY="signkey-prod-..."

4. Set your LLM API key in your environment.

Run it:

    python examples/example.py
"""

import asyncio
import os

import cognee
from cognee_community_connector_inngest import inngest_source

DATASET_NAME = "workflows_memory"
RESOURCE_NAME = "inngest_prod"

INNGEST_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
}


async def sync(key: str) -> None:
    source = inngest_source(api_key=key, resource_name=RESOURCE_NAME)
    result = await cognee.remember(source, dataset_name=DATASET_NAME, **INNGEST_REMEMBER_KWARGS)
    print("Sync result:", result)
    print("Sync stats:", source.cognee_sync_stats)


async def main():
    key = os.environ.get("INNGEST_SIGNING_KEY")
    if not key:
        print("Set INNGEST_SIGNING_KEY.\nSee the setup steps in this file's docstring, then re-run.")
        return

    print("\n=== Inngest Sync #1 (Backfill) ===")
    await sync(key)

    answer = await cognee.search(
        query_text="What functions are triggered by user signup events, and did any recent runs fail?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nWorkflow Copilot Answer:\n", answer)

    print("\n=== Inngest Sync #2 (Incremental) ===")
    await sync(key)


if __name__ == "__main__":
    asyncio.run(main())
