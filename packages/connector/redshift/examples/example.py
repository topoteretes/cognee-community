"""Redshift connector demo — turn warehouse tables into memory you can ask questions of.

Syncs a table (rows + its comments) and a query result into cognee, then asks
a question. Re-run it after changing data in Redshift: changed rows are
re-read through the timestamp cursor, and deleted rows are forgotten.

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This reads the rows you select and sends them, as text, to your configured
LLM and embedding providers during cognify. Select only the tables and
columns you mean to (``columns=``, ``where=``), and use a dedicated dataset
so you can forget it in one call.

────────────────────────────────────────────────────────────────────────────
Setup
────────────────────────────────────────────────────────────────────────────
1. Install:  cd packages/connector/redshift && uv sync
2. AWS credentials through the usual chain (``aws configure``, a profile,
   or a role), with the IAM policy from the README.
3. Point the connector at your warehouse and the table to sync:

       export REDSHIFT_DATABASE=dev
       export REDSHIFT_WORKGROUP_NAME=default-workgroup     # Serverless, or
       # export REDSHIFT_CLUSTER_IDENTIFIER=my-cluster      # provisioned
       # export REDSHIFT_DB_USER=cognee_reader              #   with IAM temporary credentials
       export AWS_REGION=us-east-1
       export REDSHIFT_EXAMPLE_TABLE=public.support_tickets
       export REDSHIFT_EXAMPLE_KEY=ticket_id
       export REDSHIFT_EXAMPLE_CURSOR=updated_at            # optional
       export LLM_API_KEY="sk-..."

   Through an SSH tunnel instead of the Data API (see the README), also set
   ``REDSHIFT_HOST``, ``REDSHIFT_PORT``, ``REDSHIFT_USER`` and
   ``REDSHIFT_PASSWORD`` and install the ``direct`` extra.

4. Run:  uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_redshift import (
    DataApiConnection,
    DirectConnection,
    RedshiftQuery,
    RedshiftTable,
    redshift_source,
)

# Keep Redshift in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "redshift"


def _connection():
    if os.getenv("REDSHIFT_HOST"):
        # A driver connection, for example through an SSH tunnel on localhost.
        return DirectConnection(
            host=os.environ["REDSHIFT_HOST"],
            port=int(os.getenv("REDSHIFT_PORT", "5439")),
            database=os.environ["REDSHIFT_DATABASE"],
            user=os.environ["REDSHIFT_USER"],
            password=os.environ["REDSHIFT_PASSWORD"],
        )
    # The Data API, configured from REDSHIFT_* and the AWS credential chain.
    return DataApiConnection.from_env()


async def main() -> None:
    table = os.getenv("REDSHIFT_EXAMPLE_TABLE")
    key = os.getenv("REDSHIFT_EXAMPLE_KEY")
    if not (os.getenv("REDSHIFT_DATABASE") and table and key):
        print(
            "Set REDSHIFT_DATABASE, REDSHIFT_EXAMPLE_TABLE and REDSHIFT_EXAMPLE_KEY "
            "(and a workgroup or cluster) to run this example. See the docstring."
        )
        return

    cursor = os.getenv("REDSHIFT_EXAMPLE_CURSOR") or None
    selections = [
        RedshiftTable(table, primary_key=key, cursor_column=cursor),
        # A query result syncs the same way. Here: how many rows each day added.
        RedshiftQuery(
            name="rows_per_day",
            sql=(
                f"SELECT TRUNC({cursor}) AS day, COUNT(*) AS new_rows FROM {table} GROUP BY 1"
                if cursor
                else f"SELECT 'all' AS day, COUNT(*) AS new_rows FROM {table}"
            ),
            primary_key="day",
        ),
    ]
    source = redshift_source(selections, connection=_connection())

    print(f"Syncing {table} from Redshift into cognee ...")
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",  # required: the source emits deletions
    )
    print("Sync stats:", source.cognee_sync_stats)

    answer = await cognee.search(
        query_text=f"What is in {table}, and what do its columns mean?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nChange or delete a row in Redshift, then re-run: changes re-sync through the "
        "cursor and deleted rows are forgotten."
    )


if __name__ == "__main__":
    asyncio.run(main())
