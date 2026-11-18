"""Sync Ahrefs SEO data into cognee memory, incrementally.

Domain Rating history and backlinks for one domain are stored as dated rows
and cognified. Re-running this script only reads what changed since the last
run, and lost backlinks are forgotten automatically.

Setup (see the README for details):
  1. Create an Ahrefs API key (Account settings > API keys). Give it a
     monthly unit limit.
  2. Set:
       AHREFS_API_KEY=...
       AHREFS_TARGET=example.com    (optional; defaults to wordcount.com,
                                     a free Ahrefs test target)
     plus the usual cognee LLM_API_KEY.

Run:
    python examples/example.py ["optional question about the domain"]
"""

import asyncio
import os
import sys

import cognee

from cognee_community_connector_ahrefs import ahrefs_source

DATASET = "ahrefs_demo"


async def main():
    target = os.environ.get("AHREFS_TARGET", "wordcount.com")
    sources = ahrefs_source(
        [target],
        reports=["domain_rating", "backlinks"],
        backlinks_limit=20,
        max_units=500,  # a sync that could cost more fails before any request
    )

    print(f"=== Sync {target} ===")
    await cognee.remember(
        sources,
        dataset_name=DATASET,
        primary_key="id",
        # "merge" is required: it makes re-runs incremental and makes
        # deletions propagate through cognee.
        write_disposition="merge",
        # The default caps a table at 50 rows; a report easily exceeds it.
        max_rows_per_table=0,
        # Each report keeps one data id across syncs; without these flags
        # cognee would skip the changed report as already added.
        incremental_loading=False,
        data_cache=False,
    )
    for source in sources:
        # Counts only, never targets or rows.
        print(f"{source.name}:", source.cognee_sync_stats)

    question = sys.argv[1] if len(sys.argv) > 1 else f"What is the Domain Rating of {target}?"
    answer = await cognee.recall(question, datasets=[DATASET])
    print("Recall:", answer)


if __name__ == "__main__":
    asyncio.run(main())
