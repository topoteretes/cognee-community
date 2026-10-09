"""ClickHouse connector demo — "ask my ClickHouse".

Pull ClickHouse tables into cognee memory, incrementally, with forget-on-delete.

This example is built on cognee's DLT ingestion subsystem: ``clickhouse_source``
returns a ``dlt`` resource that you hand straight to ``cognee.remember``. The first
run backfills the tables; re-running ``remember`` syncs only rows changed since the
cursor column's high-water mark, and rows you deleted upstream are forgotten from
memory on the next sync.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the package:

       uv sync                     # from this directory

2. Start a ClickHouse to play against (or point at your own):

       docker run -d --name cognee-clickhouse -p 8123:8123 \
         -e CLICKHOUSE_USER=default -e CLICKHOUSE_PASSWORD=clickhouse \
         -e CLICKHOUSE_DB=analytics clickhouse/clickhouse-server:24.8-alpine

3. Create a table with a key and a monotonic cursor column. Note the ``COMMENT`` —
   the connector folds it into every row's text, so it is searchable.

       CREATE TABLE analytics.events
       (
           event_id   UInt64,
           kind       LowCardinality(String),
           payload    String,
           updated_at DateTime64(3)
       )
       ENGINE = MergeTree
       ORDER BY event_id
       COMMENT 'Raw product analytics events.';

       INSERT INTO analytics.events VALUES
           (1, 'signup',   'Alphacorp onboarded through SSO', '2026-01-01 10:00:00.000'),
           (2, 'login',    'Bravocorp login failed on 2FA',  '2026-01-02 11:30:00.000');

4. Export the connection details and your LLM key, then run:

       export CLICKHOUSE_HOST=localhost
       export CLICKHOUSE_USER=default
       export CLICKHOUSE_PASSWORD=clickhouse
       export CLICKHOUSE_DATABASE=analytics
       export LLM_API_KEY=...
       uv run python examples/example.py

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This reads the contents of your ClickHouse tables. It is strictly opt-in — nothing is
fetched until you run this script. Scope what you ingest with ``tables=[...]`` and
``where=...``, and use a dedicated dataset so you can wipe it with a single
``cognee.prune``.
"""

import asyncio
import os

import cognee

from cognee_community_connector_clickhouse import clickhouse_source

# Keep ClickHouse in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "clickhouse"


def build_source():
    """Build the source. Credentials fall back to the CLICKHOUSE_* env vars."""
    return clickhouse_source(
        host=os.environ.get("CLICKHOUSE_HOST", "localhost"),
        port=int(os.environ.get("CLICKHOUSE_PORT", "8123")),
        user=os.environ.get("CLICKHOUSE_USER", "default"),
        password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
        database=os.environ.get("CLICKHOUSE_DATABASE", "analytics"),
        tables=os.environ.get("CLICKHOUSE_TABLES", "events").split(","),
        # Which columns identify a row. Omit both and the connector falls back to
        # the table's PRIMARY KEY, then its sorting key.
        key_columns={"events": "event_id"},
        # The monotonic column the incremental cursor rides on.
        cursor_columns={"events": "updated_at"},
    )


async def main() -> None:
    if not os.environ.get("LLM_API_KEY"):
        print("Set LLM_API_KEY (and the CLICKHOUSE_* vars) to run this example.")
        return

    # Start from a clean slate so the demo is reproducible.
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)

    # ── First sync: full backfill ──────────────────────────────────────────
    print("\n=== ClickHouse sync #1 (backfill) ===")
    result = await cognee.remember(
        build_source(),
        dataset_name=DATASET_NAME,
        # REQUIRED — the add pipeline defaults to "replace", which would wipe the
        # corpus the incremental cursor and delete detection diff against.
        write_disposition="merge",
    )
    print(result)

    answer = await cognee.search(
        query_text="What kinds of events are recorded here?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    # ── Second sync: incremental delta + forget-on-delete ──────────────────
    # Re-running with the SAME dataset reuses the persisted cursor: only rows at or
    # after the high-water mark are read, and anything deleted upstream is removed
    # from memory by orphan_cleanup.
    print("\n=== ClickHouse sync #2 (incremental) ===")
    print(
        "Insert or update a row with a newer updated_at, then re-run to see it\n"
        "picked up — and delete a row, then re-run to see it forgotten."
    )
    result = await cognee.remember(
        build_source(),
        dataset_name=DATASET_NAME,
        write_disposition="merge",
    )
    print(result)


if __name__ == "__main__":
    asyncio.run(main())