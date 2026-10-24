"""Grafana Loki connector demo — "ask my logs".

Pull error and warning logs from Loki into cognee memory, incrementally, with old
windows forgotten once they pass ``keep_for_days``.

This example is built on cognee's DLT ingestion subsystem: ``loki_source`` returns a
``dlt`` resource that you hand straight to ``cognee.remember``. The first run reads the
last hour; re-running ``remember`` reads only the windows that closed since.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the package:

       pip install cognee-community-connector-loki

2. Point it at a Loki. A local one:

       export LOKI_URL="http://localhost:3100"

   or Grafana Cloud (Connections → Data sources → your Loki → URL and user, plus an
   access policy token with the ``logs:read`` scope):

       export LOKI_URL="https://logs-prod-012.grafana.net"
       export LOKI_USERNAME="123456"
       export LOKI_PASSWORD="glc_…"

3. Pick what to ingest (default: every stream's error and warning lines):

       export LOKI_SELECTOR='{service_name="checkout"} |~ "(?i)error|warn"'

4. Set your LLM key (``LLM_API_KEY``) in ``.env`` like any other cognee example.

Run it:

    python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_loki import loki_source

# keep the logs in their own dataset so they are easy to inspect and forget
DATASET_NAME = "loki_logs"
# one fixed name per loki instance: it keys the sync state and the forget scope
RESOURCE_NAME = "loki_main"
DEFAULT_SELECTOR = '{service_name=~".+"} |~ "(?i)error|warn"'

# max_rows_per_table=0 lets orphan-cleanup compare against the whole synced corpus
LOKI_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
}


async def sync(url: str) -> None:
    source = loki_source(
        base_url=url,
        username=os.environ.get("LOKI_USERNAME"),
        password=os.environ.get("LOKI_PASSWORD"),
        selectors=[os.environ.get("LOKI_SELECTOR", DEFAULT_SELECTOR)],
        resource_name=RESOURCE_NAME,
    )
    result = await cognee.remember(source, dataset_name=DATASET_NAME, **LOKI_REMEMBER_KWARGS)
    print(result)
    print("sync stats:", source.cognee_sync_stats)


async def main():
    url = os.environ.get("LOKI_URL")
    if not url:
        print("Set LOKI_URL.\nSee the setup steps in this file's docstring, then re-run.")
        return

    print("\n=== Loki sync #1 (last hour) ===")
    await sync(url)

    answer = await cognee.search(
        query_text="Which services logged errors, and what went wrong?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("Logs answer:", answer)

    # the same dataset reuses the persisted cursor: only windows that closed since the
    # last run are read, and windows older than keep_for_days are forgotten
    print("\n=== Loki sync #2 (incremental) ===")
    await sync(url)


if __name__ == "__main__":
    asyncio.run(main())
