"""Zoho CRM connector demo - turn your CRM into memory.

Pull Zoho CRM records, their notes and, optionally, text attachments into cognee,
incrementally and with forget-on-delete.

Setup (see README for the Self Client steps):

    cd packages/connector/zoho-crm && uv sync --all-extras
    export ZOHO_CLIENT_ID="1000...."
    export ZOHO_CLIENT_SECRET="..."
    export ZOHO_REFRESH_TOKEN="1000...."
    export ZOHO_REGION="eu"            # com, eu, in, com.au, jp, com.cn, ca, sa
    export LLM_API_KEY="sk-..."
    uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_zoho_crm import zoho_crm_source

DATASET_NAME = "zoho_crm"


async def main() -> None:
    required = ("ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN")
    if not all(os.environ.get(name) for name in required):
        print("Set ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET and ZOHO_REFRESH_TOKEN to run this example.")
        return

    source = zoho_crm_source(
        resource_name="zoho_crm",  # one per Zoho org; keep it fixed
        client_id=os.environ["ZOHO_CLIENT_ID"],
        client_secret=os.environ["ZOHO_CLIENT_SECRET"],
        refresh_token=os.environ["ZOHO_REFRESH_TOKEN"],
        region=os.environ.get("ZOHO_REGION", "com"),
        # modules=["Leads", "Deals"], include_attachments=True, reconcile_every=6, ...
    )

    print("Syncing Zoho CRM into cognee ...")
    # primary_key="id" + write_disposition="merge" are REQUIRED: incremental runs
    # only see changes, and tombstones remove deleted records by id.
    await cognee.remember(
        source, dataset_name=DATASET_NAME, primary_key="id", write_disposition="merge"
    )
    print("Run stats:", source.cognee_sync_stats)

    answer = await cognee.search(
        query_text="Which deals are open, and what do the notes say about them?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)


if __name__ == "__main__":
    asyncio.run(main())
