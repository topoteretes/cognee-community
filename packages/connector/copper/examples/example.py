"""Copper connector demo — "ask my CRM".

Pull your Copper companies, people, opportunities and their activity log (notes,
calls, meetings) into cognee memory, incrementally, with forget-on-delete.

This example is built on cognee's DLT ingestion subsystem: ``copper_source``
returns a ``dlt`` resource that you hand straight to ``cognee.remember``. The
first run backfills the account; re-running ``remember`` re-processes only
records whose content changed, and records you delete in Copper are forgotten
from memory on the next sync.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the package:

       pip install cognee-community-connector-copper

2. In Copper open Settings → Integrations → API Keys and generate a key
   (a free trial works).

3. Export the key and the email of the user who generated it; Copper checks
   that the two belong together (the connector only searches and reads records):

       export COPPER_API_KEY="…"
       export COPPER_USER_EMAIL="you@example.com"

4. Set your LLM key (``LLM_API_KEY``) in ``.env`` like any other cognee example.

Run it:

    python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_copper import copper_source

# keep the crm in its own dataset so it is easy to inspect and forget
DATASET_NAME = "copper_crm"
# one fixed name per copper account: it keys the sync state and the forget-on-delete scope
RESOURCE_NAME = "copper_crm_main"

# max_rows_per_table=0 lets orphan-cleanup compare against the whole synced corpus
COPPER_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
}


async def sync(api_key: str, user_email: str) -> None:
    source = copper_source(api_key=api_key, user_email=user_email, resource_name=RESOURCE_NAME)
    result = await cognee.remember(source, dataset_name=DATASET_NAME, **COPPER_REMEMBER_KWARGS)
    print(result)
    print("sync stats:", source.cognee_sync_stats)


async def main():
    api_key = os.environ.get("COPPER_API_KEY")
    user_email = os.environ.get("COPPER_USER_EMAIL")
    if not api_key or not user_email:
        print(
            "Set COPPER_API_KEY and COPPER_USER_EMAIL.\n"
            "See the setup steps in this file's docstring, then re-run."
        )
        return

    print("\n=== Copper sync #1 (backfill) ===")
    await sync(api_key, user_email)

    answer = await cognee.search(
        query_text="Which open opportunities have notes about blockers, and who is the contact?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("CRM answer:", answer)

    # the same dataset reuses the persisted state: only changed records are processed,
    # and anything deleted in copper is removed from memory
    print("\n=== Copper sync #2 (incremental) ===")
    await sync(api_key, user_email)


if __name__ == "__main__":
    asyncio.run(main())
