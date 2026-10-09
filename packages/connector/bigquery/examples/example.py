"""BigQuery connector demo — turn your warehouse's metadata into memory.

Sync dataset, table, view and column descriptions (and, optionally, rows of one
table) from BigQuery into cognee, then ask about them. Re-run after changing a
description, deleting a row or dropping a table to see the re-sync and
forget-on-delete.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Create a service account with BigQuery Data Viewer + BigQuery Job User and
   download its JSON key. The free BigQuery sandbox works.
2. Export the key, the project and your LLM key, then run:

       export GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json
       export BIGQUERY_PROJECT=my-project
       export BIGQUERY_DATASETS=analytics            # optional, comma-separated
       export BIGQUERY_ROW_TABLE=analytics.customers # optional: also ingest rows
       export BIGQUERY_ROW_KEY=customer_id           #   ... keyed by this column
       export BIGQUERY_ROW_CURSOR=updated_at         #   ... optional incremental cursor
       export LLM_API_KEY=sk-...
       uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_bigquery import RowSync, bigquery_source

# Keep BigQuery in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "bigquery"


async def main() -> None:
    project = os.environ.get("BIGQUERY_PROJECT")
    if not project or not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        print("Set BIGQUERY_PROJECT and GOOGLE_APPLICATION_CREDENTIALS to run this example.")
        return

    datasets = [d for d in os.environ.get("BIGQUERY_DATASETS", "").split(",") if d] or None
    row_syncs = []
    if os.environ.get("BIGQUERY_ROW_TABLE") and os.environ.get("BIGQUERY_ROW_KEY"):
        row_syncs.append(
            RowSync(
                table=os.environ["BIGQUERY_ROW_TABLE"],
                key_column=os.environ["BIGQUERY_ROW_KEY"],
                cursor_column=os.environ.get("BIGQUERY_ROW_CURSOR") or None,
            )
        )

    print("Syncing BigQuery metadata into cognee ...")
    await cognee.remember(
        bigquery_source(project=project, datasets=datasets, row_syncs=row_syncs),
        dataset_name=DATASET_NAME,
        write_disposition="merge",
        self_improvement=False,
    )

    answer = await cognee.search(
        query_text="Which tables are there, and what do their columns describe?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)


if __name__ == "__main__":
    asyncio.run(main())
