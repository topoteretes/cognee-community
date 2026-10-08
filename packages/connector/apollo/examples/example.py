"""Apollo.io connector demo — "ask my CRM".

Pull your Apollo contacts, accounts and sequence activity into cognee memory,
incrementally, with forget-on-delete.

This example is built on cognee's DLT ingestion subsystem: ``apollo_source``
returns a ``dlt`` resource that you hand straight to ``cognee.remember``. The
first run backfills the workspace; re-running ``remember`` re-processes only
records whose content changed, and records you delete in Apollo are forgotten
from memory on the next sync.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the package:

       pip install cognee-community-connector-apollo

2. Create an API key in Apollo under Settings → Integrations → API (a paid plan
   or a trial; see the README for the scopes a scoped key needs).

3. Export it (the key is read-only here, the connector only lists and reads):

       export APOLLO_API_KEY="…"

4. Set your LLM key (``LLM_API_KEY``) in ``.env`` like any other cognee example.

Run it:

    python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_apollo import apollo_source

# keep the crm in its own dataset so it is easy to inspect and forget
DATASET_NAME = "apollo_crm"

# max_rows_per_table=0 lets orphan-cleanup compare against the whole synced corpus
APOLLO_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
}


async def sync(api_key: str) -> None:
    source = apollo_source(api_key=api_key)
    result = await cognee.remember(source, dataset_name=DATASET_NAME, **APOLLO_REMEMBER_KWARGS)
    print(result)
    print("sync stats:", source.cognee_sync_stats)


async def main():
    api_key = os.environ.get("APOLLO_API_KEY")
    if not api_key:
        print("Set APOLLO_API_KEY.\nSee the setup steps in this file's docstring, then re-run.")
        return

    print("\n=== Apollo sync #1 (backfill) ===")
    await sync(api_key)

    answer = await cognee.search(
        query_text="Which contacts are enrolled in a sequence, and at which companies?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("CRM answer:", answer)

    # the same dataset reuses the persisted state: only changed records are processed,
    # and anything deleted in apollo is removed from memory
    print("\n=== Apollo sync #2 (incremental) ===")
    await sync(api_key)


if __name__ == "__main__":
    asyncio.run(main())
