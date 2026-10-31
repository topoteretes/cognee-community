"""Ingest Egnyte folders into cognee memory, incrementally.

Demonstrates the Egnyte DLT connector: text, Markdown, CSV, JSON, PDF and Word
files under the configured folders (recursively) are extracted, chunked and
cognified like any other document.  Re-running this script only re-processes
files that changed since the last run, and files deleted from Egnyte (or moved
out of the configured folders) are forgotten automatically.

Install:
    pip install cognee-community-connector-egnyte

Setup:
    1. Request an API key for an "internal application" on your domain at
       https://developers.egnyte.com.
    2. Set:
         EGNYTE_DOMAIN=<your domain, e.g. acme>
         EGNYTE_FOLDER_PATHS=/Shared/Projects
       and either
         EGNYTE_ACCESS_TOKEN=<token from get_access_token(...)>
       or
         EGNYTE_API_KEY, EGNYTE_USERNAME and EGNYTE_PASSWORD
       Storing a token is better: Egnyte limits token requests per hour.

Also set the usual cognee LLM_API_KEY (see .env.template); this example calls
cognee.recall(), which needs an LLM for the final answer.
"""

import asyncio

import cognee

from cognee_community_connector_egnyte import egnyte_source

DATASET_NAME = "egnyte_demo"


async def sync():
    return await cognee.remember(
        egnyte_source(),  # reads EGNYTE_* env vars
        dataset_name=DATASET_NAME,
        primary_key="id",
        # "merge" is required: it's what makes re-runs incremental and what
        # makes deletions propagate via orphan cleanup.
        write_disposition="merge",
        # The DLT ingestion default caps a table at 50 rows; Egnyte folders
        # commonly exceed that, so lift the cap.
        max_rows_per_table=0,
    )


async def main():
    print("=== Initial sync ===")
    print(await sync())

    answer = await cognee.recall("Summarize what's in the Egnyte folders.")
    print("Recall:", answer)

    print("\n=== Incremental re-sync (only changed or deleted files are processed) ===")
    print(await sync())


if __name__ == "__main__":
    asyncio.run(main())
