"""Pipedrive connector demo — "ask my CRM".

Pull your Pipedrive deals, people, organizations, activities and notes into
cognee memory, incrementally, with forget-on-delete.

This example is built on cognee's DLT ingestion subsystem: ``pipedrive_source``
returns a ``dlt`` resource that you hand straight to ``cognee.remember``. The
first run backfills the company; re-running ``remember`` re-processes only
records whose content changed, and records you delete in Pipedrive are forgotten
from memory on the next sync.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the package:

       pip install cognee-community-connector-pipedrive

2. Copy your personal API token in Pipedrive under
   Settings → Personal preferences → API (any plan, including a developer sandbox).

3. Export it (the connector only lists and reads records):

       export PIPEDRIVE_API_TOKEN="…"

4. Set your LLM key (``LLM_API_KEY``) in ``.env`` like any other cognee example.

Run it:

    python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_pipedrive import pipedrive_source

# keep the crm in its own dataset so it is easy to inspect and forget
DATASET_NAME = "pipedrive_crm"
# one fixed name per pipedrive company: it keys the sync state and the forget-on-delete scope
RESOURCE_NAME = "pipedrive_crm_main"

# max_rows_per_table=0 lets orphan-cleanup compare against the whole synced corpus
PIPEDRIVE_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
}


async def sync(api_token: str) -> None:
    source = pipedrive_source(api_token=api_token, resource_name=RESOURCE_NAME)
    result = await cognee.remember(source, dataset_name=DATASET_NAME, **PIPEDRIVE_REMEMBER_KWARGS)
    print(result)
    print("sync stats:", source.cognee_sync_stats)


async def main():
    api_token = os.environ.get("PIPEDRIVE_API_TOKEN")
    if not api_token:
        print(
            "Set PIPEDRIVE_API_TOKEN.\nSee the setup steps in this file's docstring, then re-run."
        )
        return

    print("\n=== Pipedrive sync #1 (backfill) ===")
    await sync(api_token)

    answer = await cognee.search(
        query_text="Which open deals have notes about blockers, and who is the contact?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("CRM answer:", answer)

    # the same dataset reuses the persisted state: only changed records are processed,
    # and anything deleted in pipedrive is removed from memory
    print("\n=== Pipedrive sync #2 (incremental) ===")
    await sync(api_token)


if __name__ == "__main__":
    asyncio.run(main())
