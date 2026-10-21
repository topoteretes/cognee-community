"""Axiom connector demo — "Ask my observability stack & alert monitors".

Pull your Axiom datasets, monitor definitions, APL queries, and notifiers into cognee memory,
incrementally, with automated token redaction and forget-on-delete.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the connector package:

       pip install cognee-community-connector-axiom

2. Generate an API Token or Personal Token in Axiom:
   Axiom Dashboard → Settings → API Tokens.

3. Export it:

       export AXIOM_TOKEN="xapt-..."

4. Set your LLM API key in your environment.

Run it:

    python examples/example.py
"""

import asyncio
import os

import cognee
from cognee_community_connector_axiom import axiom_source

DATASET_NAME = "observability_memory"
RESOURCE_NAME = "axiom_prod"

AXIOM_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
}


async def sync(token: str) -> None:
    source = axiom_source(api_key=token, resource_name=RESOURCE_NAME)
    result = await cognee.remember(source, dataset_name=DATASET_NAME, **AXIOM_REMEMBER_KWARGS)
    print("Sync result:", result)
    print("Sync stats:", source.cognee_sync_stats)


async def main():
    token = os.environ.get("AXIOM_TOKEN")
    if not token:
        print("Set AXIOM_TOKEN.\nSee the setup steps in this file's docstring, then re-run.")
        return

    print("\n=== Axiom Sync #1 (Backfill) ===")
    await sync(token)

    answer = await cognee.search(
        query_text="What datasets exist, and what monitors are watching for high error rates?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nObservability Copilot Answer:\n", answer)

    print("\n=== Axiom Sync #2 (Incremental) ===")
    await sync(token)


if __name__ == "__main__":
    asyncio.run(main())
