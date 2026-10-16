"""ClickHouse connector demo — "ask my warehouse".

Pull rows from a ClickHouse table into cognee memory, incrementally, with
forget-on-delete.

This example is built on cognee's DLT ingestion subsystem: ``clickhouse_source``
returns a ``dlt`` source that you hand straight to ``cognee.remember``. The
first run backfills the table; re-running ``remember`` syncs only rows changed
since (via your chosen ``cursor_column``), and rows you delete in ClickHouse
are forgotten from memory on the next sync.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the extra:

       pip install "cognee[clickhouse]"     # or: uv sync --extra clickhouse

2. Have a ClickHouse server reachable over HTTP(S) with a username/password,
   and pick a monotonic column on your table to use as the incremental
   cursor (a timestamp like ``updated_at``, or an auto-increment id).

3. Export your connection details:

       export CLICKHOUSE_HOST="localhost"
       export CLICKHOUSE_PORT="8123"
       export CLICKHOUSE_USER="default"
       export CLICKHOUSE_PASSWORD="…"
       export CLICKHOUSE_DATABASE="analytics"
       export CLICKHOUSE_TABLE="support_tickets"
       export CLICKHOUSE_CURSOR_COLUMN="updated_at"

4. Set your LLM key (``LLM_API_KEY``) in ``.env`` like any other cognee example.

Run it:

    uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_clickhouse import clickhouse_source

# Keep the table in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "clickhouse_table"

# Routing kwargs shared by every remember() call below. ``max_rows_per_table=0``
# disables cognee's per-table read cap so orphan-cleanup (forget-on-delete)
# compares against the *entire* synced corpus, not a 50-row window.
CLICKHOUSE_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
}


async def main():
    host = os.environ.get("CLICKHOUSE_HOST")
    port = os.environ.get("CLICKHOUSE_PORT")
    user = os.environ.get("CLICKHOUSE_USER")
    password = os.environ.get("CLICKHOUSE_PASSWORD")
    database = os.environ.get("CLICKHOUSE_DATABASE")
    table = os.environ.get("CLICKHOUSE_TABLE")
    cursor_column = os.environ.get("CLICKHOUSE_CURSOR_COLUMN")

    if not all([database, table, cursor_column]):
        print(
            "Set CLICKHOUSE_DATABASE, CLICKHOUSE_TABLE and CLICKHOUSE_CURSOR_COLUMN "
            "(plus CLICKHOUSE_HOST/PORT/USER/PASSWORD as needed).\n"
            "See the setup steps in this file's docstring, then re-run."
        )
        return

    # Start from a clean slate so the demo is reproducible.
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)

    def build_source():
        return clickhouse_source(
            host=host,
            port=int(port) if port else None,
            user=user,
            password=password,
            database=database,
            table=table,
            cursor_column=cursor_column,
        )

    # ── First sync: full backfill ──────────────────────────────────────────
    print("\n=== ClickHouse sync #1 (backfill) ===")
    result = await cognee.remember(
        build_source(), dataset_name=DATASET_NAME, **CLICKHOUSE_REMEMBER_KWARGS
    )
    print(result)

    answer = await cognee.search(
        query_text="Summarize what's in this table.",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("Table summary:", answer)

    # ── Second sync: incremental delta + forget-on-delete ──────────────────
    # Re-running with the SAME dataset reuses the persisted cursor: only rows
    # changed since sync #1 are fetched, and anything deleted in ClickHouse is
    # removed from memory by orphan_cleanup.
    print("\n=== ClickHouse sync #2 (incremental) ===")
    result = await cognee.remember(
        build_source(), dataset_name=DATASET_NAME, **CLICKHOUSE_REMEMBER_KWARGS
    )
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
