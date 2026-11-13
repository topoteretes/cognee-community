"""BigQuery connector demo â€” turn BigQuery schemas and records into AI memory.

Sync BigQuery dataset/table schemas, descriptions, and query results into cognee.
``bigquery_source`` returns a ``dlt`` resource you hand straight to ``cognee.remember``.
Schemas and records flow through normal cognify entity-extraction into the
knowledge graph.

Metadata (table descriptions, columns, types, modes) is often more valuable to
an AI memory layer than raw millions of rows.

Prerequisites:
    1. Install dependencies:
       pip install google-cloud-bigquery google-auth
    2. Provide Google Cloud Service Account credentials:
       export BIGQUERY_CREDENTIALS_PATH="/path/to/service-account.json"
       export BIGQUERY_PROJECT_ID="your-gcp-project"
       export LLM_API_KEY="sk-..."
"""

import asyncio
import os

import cognee

from cognee_community_connector_bigquery import bigquery_source

DATASET_NAME = "bigquery_analytics"


async def main() -> None:
    if not os.getenv("BIGQUERY_CREDENTIALS_PATH") and not os.getenv(
        "GOOGLE_APPLICATION_CREDENTIALS"
    ):
        print("Note: Run with BIGQUERY_CREDENTIALS_PATH set to your GCP Service Account JSON key.")
        return

    # Ingest table schemas and column descriptions from the 'analytics' dataset
    source = bigquery_source(
        dataset_id="analytics",
        include_metadata=True,
    )

    print("Syncing BigQuery metadata into cognee memory ...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    answer = await cognee.search(
        query_text="What tables and columns exist in the analytics dataset?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)


if __name__ == "__main__":
    asyncio.run(main())
