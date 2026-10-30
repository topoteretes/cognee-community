"""Sync Snowflake tables into cognee memory, incrementally.

Each selected table becomes a document from its comments and keys, and each of
its rows becomes a document. Re-running this script only reads tables that
changed since the last run, and deleted rows are forgotten automatically.

Setup (see the README for the key pair):
  1. Set:
       SNOWFLAKE_ACCOUNT=ORGNAME-ACCOUNTNAME
       SNOWFLAKE_USER=<login name, on trial accounts your email>
       SNOWFLAKE_PRIVATE_KEY_PATH=~/.snowflake/rsa_key.p8
       SNOWFLAKE_WAREHOUSE=COMPUTE_WH
       SNOWFLAKE_TABLES=SALES_DB.PUBLIC.CUSTOMERS,SALES_DB.PUBLIC.ORDERS
     plus the usual cognee LLM settings, e.g. LLM_API_KEY.
  2. Check the login once:  python examples/check_connection.py

Run:
    python examples/example.py ["optional question about your data"]
    python examples/example.py --full-refresh   (reads every table again)
"""

import asyncio
import sys

import cognee

from cognee_community_connector_snowflake import snowflake_source

DATASET = "snowflake_demo"


async def main():
    args = [arg for arg in sys.argv[1:] if arg != "--full-refresh"]
    # Reads the SNOWFLAKE_* settings and SNOWFLAKE_TABLES.
    source = snowflake_source(full_refresh="--full-refresh" in sys.argv)

    print("=== Sync ===")
    await cognee.remember(
        source,
        dataset_name=DATASET,
        primary_key="id",
        # "merge" is required: it makes re-runs incremental and makes
        # deletions propagate through cognee's orphan cleanup.
        write_disposition="merge",
        # cognee's staging cap is 50 rows by default; the connector has its own
        # per-table cap (max_rows), so this one is turned off.
        max_rows_per_table=0,
    )
    # Counts only, never names or values.
    print("Sync stats:", source.cognee_sync_stats)

    question = args[0] if args else "Which customers have open orders, and for how much?"
    answer = await cognee.recall(question, datasets=[DATASET])
    print("Recall:", answer)


if __name__ == "__main__":
    asyncio.run(main())
