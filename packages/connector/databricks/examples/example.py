"""Sync Databricks notebooks, Unity Catalog tables and named queries into cognee.

Each notebook becomes a document with the tables it reads and writes, and each
table one with the notebooks that use it, joined through Unity Catalog lineage.
Re-running this script only exports notebooks and re-sends tables that changed,
and deleted notebooks and dropped tables are forgotten.

Setup (see the README):
  1. Set DATABRICKS_HOST and DATABRICKS_TOKEN, DATABRICKS_PATHS and/or
     DATABRICKS_TABLES, plus the usual cognee LLM settings, e.g. LLM_API_KEY.
  2. Check them once:  python examples/check_connection.py

Run:
    python examples/example.py ["optional question about your workspace"]
    python examples/example.py --full-refresh   (exports and re-sends everything)
    python examples/example.py --query open_orders="SELECT ... ORDER BY order_id"
        (named queries run on DATABRICKS_WAREHOUSE_ID; repeat --query for more)
"""

import argparse
import asyncio

import cognee

from cognee_community_connector_databricks import databricks_source

DATASET = "databricks_demo"


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("question", nargs="?", default="Which notebooks build which tables?")
    parser.add_argument("--full-refresh", action="store_true")
    parser.add_argument("--query", action="append", default=[], metavar="NAME=SQL")
    return parser.parse_args()


async def main():
    args = parse_args()
    queries = dict(query.split("=", 1) for query in args.query)
    # Reads DATABRICKS_HOST, DATABRICKS_TOKEN, DATABRICKS_PATHS, DATABRICKS_TABLES
    # and, for queries, DATABRICKS_WAREHOUSE_ID.
    source = databricks_source(queries=queries, full_refresh=args.full_refresh)

    print("=== Sync ===")
    await cognee.remember(
        source,
        dataset_name=DATASET,
        primary_key="id",
        # "merge" is required: it makes re-runs incremental and makes
        # deletions propagate through cognee's orphan cleanup.
        write_disposition="merge",
        # cognee's staging cap is 50 rows by default; a workspace can have more
        # notebooks and tables than that, so the cap is turned off.
        max_rows_per_table=0,
    )
    # Counts only, never names or content.
    print("Sync stats:", source.cognee_sync_stats)

    answer = await cognee.recall(args.question, datasets=[DATASET])
    print("Recall:", answer)


if __name__ == "__main__":
    asyncio.run(main())
