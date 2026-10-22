"""Doppler connector demo — "Ask my infrastructure configs & audit logs".

Pull your Doppler projects, environments, config variable names, and audit logs into cognee memory,
incrementally, with zero secret leakage and forget-on-delete.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the connector package:

       pip install cognee-community-connector-doppler

2. Generate a Service Token in Doppler: Project Settings → Service Tokens.

3. Export it:

       export DOPPLER_TOKEN="dp.st.production_..."

4. Set your LLM API key in your environment.

Run it:

    python examples/example.py
"""

import asyncio
import os

import cognee
from cognee_community_connector_doppler import doppler_source

DATASET_NAME = "devops_memory"
RESOURCE_NAME = "doppler_main"

DOPPLER_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
}


async def sync(token: str) -> None:
    source = doppler_source(api_key=token, resource_name=RESOURCE_NAME)
    result = await cognee.remember(source, dataset_name=DATASET_NAME, **DOPPLER_REMEMBER_KWARGS)
    print("Sync result:", result)
    print("Sync stats:", source.cognee_sync_stats)


async def main():
    token = os.environ.get("DOPPLER_TOKEN")
    if not token:
        print("Set DOPPLER_TOKEN.\nSee the setup steps in this file's docstring, then re-run.")
        return

    print("\n=== Doppler Sync #1 (Backfill) ===")
    await sync(token)

    answer = await cognee.search(
        query_text="What environments exist for project api-service, and what variable keys are configured?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nDevOps Assistant Answer:\n", answer)

    print("\n=== Doppler Sync #2 (Incremental) ===")
    await sync(token)


if __name__ == "__main__":
    asyncio.run(main())
