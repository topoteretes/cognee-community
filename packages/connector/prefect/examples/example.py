"""Prefect workflow orchestration connector demo — "ask my orchestrator".

Pull your Prefect flows, deployments, and flow run history into cognee memory,
incrementally, with forget-on-delete and zero parameter secret leakage.

This example uses cognee's DLT ingestion subsystem: ``prefect_source``
returns a ``dlt`` resource handed directly to ``cognee.remember``. The
first run backfills the orchestration metadata; re-running ``remember``
re-processes only entities whose state or metadata changed, and entities deleted
in Prefect are pruned from memory on the subsequent sync.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install dependencies:

       pip install cognee-community-connector-prefect

2. If using self-hosted Prefect, ensure your server is running (default: http://localhost:4200/api).
   If using Prefect Cloud:

       export PREFECT_API_URL="https://api.prefect.cloud/api/accounts/<account-id>/workspaces/<workspace-id>"
       export PREFECT_API_KEY="pnu_..."

3. Set your LLM API key (e.g. ``LLM_API_KEY``) in your environment.

Run it:

    python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_prefect import prefect_source

DATASET_NAME = "prefect_orchestration"
RESOURCE_NAME = "prefect_workspace_main"

PREFECT_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
}


async def sync(base_url: str, api_key: str | None = None) -> None:
    source = prefect_source(
        base_url=base_url,
        api_key=api_key,
        resource_name=RESOURCE_NAME,
        include=("flows", "deployments", "flow_runs"),
    )
    result = await cognee.remember(source, dataset_name=DATASET_NAME, **PREFECT_REMEMBER_KWARGS)
    print("Ingestion result:", result)
    print("Sync stats:", source.cognee_sync_stats)


async def main():
    base_url = os.environ.get("PREFECT_API_URL", "http://localhost:4200/api")
    api_key = os.environ.get("PREFECT_API_KEY")

    print(f"\n=== Prefect sync #1 (backfill from {base_url}) ===")
    await sync(base_url, api_key)

    answer = await cognee.search(
        query_text="What flows and deployments failed or have active schedules?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nOrchestrator answer:", answer)

    print("\n=== Prefect sync #2 (incremental update) ===")
    await sync(base_url, api_key)


if __name__ == "__main__":
    asyncio.run(main())
