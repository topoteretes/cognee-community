"""Sync a Sanity dataset into cognee memory, incrementally.

Published documents of the chosen types are turned into documents and cognified.
Re-running this script only processes what changed since the last run, and
documents deleted or unpublished in Sanity are forgotten automatically.

Setup:
  1. sanity.io/manage -> your project -> API -> Tokens -> Add API token (Viewer).
  2. Set:
       SANITY_PROJECT_ID=abc123xy
       SANITY_DOCUMENT_TYPES=article,author
       SANITY_TOKEN=sk...                  (not needed for a public dataset)
       SANITY_DATASET=production           (optional, this is the default)
     plus the usual cognee LLM_API_KEY.

Run:
    uv run python examples/example.py ["optional question about your content"]
"""

import asyncio
import os
import sys

import cognee
from cognee.modules.retrieval.exceptions.exceptions import NoDataError

from cognee_community_connector_sanity import sanity_source

DATASET = "sanity_demo"


async def main():
    project_id = os.environ.get("SANITY_PROJECT_ID")
    types = [t for t in os.environ.get("SANITY_DOCUMENT_TYPES", "").split(",") if t.strip()]
    if not project_id or not types:
        print("Set SANITY_PROJECT_ID and SANITY_DOCUMENT_TYPES first (see the top of this file).")
        return

    source = sanity_source(
        project_id=project_id,
        dataset=os.environ.get("SANITY_DATASET", "production"),
        document_types=types,
        token=os.environ.get("SANITY_TOKEN"),
    )
    await cognee.remember(
        source,
        dataset_name=DATASET,
        primary_key="id",
        # "merge" is required: it makes re-runs incremental and makes
        # deletions propagate through cognee's orphan cleanup.
        write_disposition="merge",
        # The default caps a table at 50 rows; a dataset easily exceeds it.
        max_rows_per_table=0,
    )
    # Counts only, never titles or content.
    print("sync stats:", source.cognee_sync_stats)

    question = sys.argv[1] if len(sys.argv) > 1 else "What is this content about?"
    try:
        print("answer:", await cognee.recall(question, datasets=[DATASET]))
    except NoDataError:  # nothing synced yet
        print("Nothing to search yet: no published documents of those types.")


if __name__ == "__main__":
    asyncio.run(main())
