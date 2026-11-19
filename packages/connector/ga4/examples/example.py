"""Google Analytics 4 connector demo — "what does my analytics setup mean?".

Pull a GA4 property's custom dimensions, custom metrics, key events, audience
definitions and reporting annotations into cognee memory, incrementally, with
forget-on-delete. Report data is never read.

This example is built on cognee's DLT ingestion subsystem: ``ga4_source`` returns
a ``dlt`` resource that you hand straight to ``cognee.remember``. The first run
backfills the property; re-running ``remember`` re-processes only definitions whose
content changed, and definitions you archive or delete in GA4 are forgotten from
memory on the next sync.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the package:

       pip install cognee-community-connector-ga4

2. Create a service account, enable the Google Analytics Admin API in its Google
   Cloud project and add its email to the property as a Viewer (see the README).

3. Export the key file and the numeric property id (Admin → Property details):

       export GOOGLE_APPLICATION_CREDENTIALS="/path/to/service-account.json"
       export GA4_PROPERTY_ID="123456789"

4. Set your LLM key (``LLM_API_KEY``) in ``.env`` like any other cognee example.

Run it:

    python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_ga4 import ga4_source

# keep the analytics setup in its own dataset so it is easy to inspect and forget
DATASET_NAME = "ga4_analytics"

# max_rows_per_table=0 lets orphan-cleanup compare against the whole synced corpus
GA4_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
}


async def sync(property_id: str) -> None:
    # credentials come from GOOGLE_APPLICATION_CREDENTIALS (application default credentials)
    source = ga4_source(property_id)
    result = await cognee.remember(source, dataset_name=DATASET_NAME, **GA4_REMEMBER_KWARGS)
    print(result)
    print("sync stats:", source.cognee_sync_stats)


async def main():
    property_id = os.environ.get("GA4_PROPERTY_ID")
    if not property_id:
        print(
            "Set GA4_PROPERTY_ID (and GOOGLE_APPLICATION_CREDENTIALS).\n"
            "See the setup steps in this file's docstring, then re-run."
        )
        return

    print("\n=== GA4 sync #1 (backfill) ===")
    await sync(property_id)

    answer = await cognee.search(
        query_text="Which audiences and key events track purchases, and how are they defined?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("Analytics answer:", answer)

    # the same dataset reuses the persisted state: only changed definitions are processed,
    # and anything archived or deleted in ga4 is removed from memory
    print("\n=== GA4 sync #2 (incremental) ===")
    await sync(property_id)


if __name__ == "__main__":
    asyncio.run(main())
