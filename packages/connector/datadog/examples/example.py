"""Datadog connector demo: "ask your on-call knowledge".

Pull your Datadog monitors, dashboards, notebooks, SLOs and incidents into cognee
memory, incrementally, with forget-on-delete.

This example is built on cognee's DLT ingestion subsystem: ``datadog_source``
returns a ``dlt`` resource that you hand straight to ``cognee.remember``. The
first run backfills the org; re-running ``remember`` re-processes only items
whose content changed, and items you delete in Datadog are forgotten from memory
on the next sync.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the package:

       pip install cognee-community-connector-datadog

2. Create an API key and an application key in Datadog under Organization
   Settings (see the README for the read-only scopes the application key needs).

3. Export them, with the site your org lives on (the connector only reads):

       export DD_API_KEY="…"
       export DD_APP_KEY="…"
       export DD_SITE="datadoghq.com"   # or datadoghq.eu, us5.datadoghq.com, …

4. Set your LLM key (``LLM_API_KEY``) in ``.env`` like any other cognee example.

Run it:

    python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_datadog import datadog_source

# keep the org in its own dataset so it is easy to inspect and forget
DATASET_NAME = "datadog_org"
# one fixed name per datadog org: it keys the sync state and the forget-on-delete scope
RESOURCE_NAME = "datadog_org_main"

# max_rows_per_table=0 lets orphan-cleanup compare against the whole synced corpus
DATADOG_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
}


async def sync(api_key: str, app_key: str, site: str) -> None:
    source = datadog_source(
        api_key=api_key, app_key=app_key, site=site, resource_name=RESOURCE_NAME
    )
    result = await cognee.remember(source, dataset_name=DATASET_NAME, **DATADOG_REMEMBER_KWARGS)
    print(result)
    print("sync stats:", source.cognee_sync_stats)


async def main():
    api_key = os.environ.get("DD_API_KEY")
    app_key = os.environ.get("DD_APP_KEY")
    site = os.environ.get("DD_SITE", "datadoghq.com")
    if not api_key or not app_key:
        print(
            "Set DD_API_KEY and DD_APP_KEY (and DD_SITE if your org is not on US1).\n"
            "See the setup steps in this file's docstring, then re-run."
        )
        return

    print("\n=== Datadog sync #1 (backfill) ===")
    await sync(api_key, app_key, site)

    answer = await cognee.search(
        query_text="Which monitors have a runbook, and what do they say to do?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("On-call answer:", answer)

    # the same dataset reuses the persisted state: only changed items are processed,
    # and anything deleted in datadog is removed from memory
    print("\n=== Datadog sync #2 (incremental) ===")
    await sync(api_key, app_key, site)


if __name__ == "__main__":
    asyncio.run(main())
