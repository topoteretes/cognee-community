"""Amplitude connector demo — "ask my analytics".

Pull your Amplitude event taxonomy, cohort definitions, chart annotations and the
saved charts you name into cognee memory, incrementally, with forget-on-delete.
Raw events are never read.

This example is built on cognee's DLT ingestion subsystem: ``amplitude_source``
returns a ``dlt`` resource that you hand straight to ``cognee.remember``. The
first run backfills the project; re-running ``remember`` re-processes only
records whose content changed, and records you delete or archive in Amplitude
are forgotten from memory on the next sync.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the package:

       pip install cognee-community-connector-amplitude

2. Copy the project's API key and generate a secret key in Amplitude under
   Settings → Projects → your project → General (see the README).

3. Export them (they are read-only here, the connector only sends GET requests):

       export AMPLITUDE_API_KEY="…"
       export AMPLITUDE_SECRET_KEY="…"
       # optional, for projects in the EU data region: export AMPLITUDE_REGION="eu"
       # optional, saved charts to include: export AMPLITUDE_CHART_IDS="abc123,def456"

4. Set your LLM key (``LLM_API_KEY``) in ``.env`` like any other cognee example.

Run it:

    python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_amplitude import amplitude_source

# keep the analytics metadata in its own dataset so it is easy to inspect and forget
DATASET_NAME = "amplitude_analytics"

# max_rows_per_table=0 lets orphan-cleanup compare against the whole synced corpus
AMPLITUDE_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
}


async def sync(api_key: str, secret_key: str) -> None:
    chart_ids = os.environ.get("AMPLITUDE_CHART_IDS")
    source = amplitude_source(
        api_key=api_key,
        secret_key=secret_key,
        region=os.environ.get("AMPLITUDE_REGION", "us"),
        chart_ids=chart_ids.split(",") if chart_ids else None,
    )
    result = await cognee.remember(source, dataset_name=DATASET_NAME, **AMPLITUDE_REMEMBER_KWARGS)
    print(result)
    print("sync stats:", source.cognee_sync_stats)


async def main():
    api_key = os.environ.get("AMPLITUDE_API_KEY")
    secret_key = os.environ.get("AMPLITUDE_SECRET_KEY")
    if not (api_key and secret_key):
        print(
            "Set AMPLITUDE_API_KEY and AMPLITUDE_SECRET_KEY.\n"
            "See the setup steps in this file's docstring, then re-run."
        )
        return

    print("\n=== Amplitude sync #1 (backfill) ===")
    await sync(api_key, secret_key)

    answer = await cognee.search(
        query_text="Which events track checkout, and what do their properties mean?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("Analytics answer:", answer)

    # the same dataset reuses the persisted state: only changed records are processed,
    # and anything deleted or archived in amplitude is removed from memory
    print("\n=== Amplitude sync #2 (incremental) ===")
    await sync(api_key, secret_key)


if __name__ == "__main__":
    asyncio.run(main())
