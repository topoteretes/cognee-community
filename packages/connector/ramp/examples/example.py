"""Ramp connector demo - ask questions about your company's spend.

Each card transaction becomes one document written around its memo, with the card
holder, merchant, trip, attendees and receipt line items.

Each run is incremental: only transactions whose document changed are re-ingested,
and ones Ramp drops or declines within the lookback window are forgotten.

Setup:
1. In Ramp, an admin creates a developer app (Company -> Developer -> Apps; on the
   sandbox, demo.ramp.com/settings/ramp-developer/apps), keeps client credentials
   on, and allows only the ``transactions:read`` and ``receipts:read`` scopes.
2. Run:

       export RAMP_CLIENT_ID="..."
       export RAMP_CLIENT_SECRET="..."
       export RAMP_SANDBOX=1          # optional: use demo-api.ramp.com
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_ramp import ramp_source

DATASET_NAME = "ramp"


async def main() -> None:
    client_id = os.environ.get("RAMP_CLIENT_ID")
    client_secret = os.environ.get("RAMP_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("Set RAMP_CLIENT_ID and RAMP_CLIENT_SECRET to run this example.")
        return

    source = ramp_source(
        resource_name="ramp_example",  # keep fixed: it scopes the sync state
        client_id=client_id,
        client_secret=client_secret,
        sandbox=os.environ.get("RAMP_SANDBOX", "").lower() in ("1", "true", "yes"),
    )
    print("Syncing Ramp transactions into cognee ...")
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",
        self_improvement=False,  # no enrichment pass on every scheduled sync
    )
    print("Sync stats:", source.cognee_sync_stats)

    answer = await cognee.search(
        query_text="What were our biggest team events, and who attended them?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\n", answer)


if __name__ == "__main__":
    asyncio.run(main())
