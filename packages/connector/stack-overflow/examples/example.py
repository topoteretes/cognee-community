"""Stack Overflow connector demo — "ask my Q&A".

Pull Stack Overflow questions and answers into cognee memory, incrementally,
with forget-on-delete.

This example is built on cognee's DLT ingestion subsystem:
``stack_overflow_source`` returns a ``dlt`` resource you hand straight to
``cognee.remember``.  The first run backfills all questions for the given
tags; re-running ``remember`` syncs only questions modified since the last
run, and questions that disappear (deleted / migrated) are forgotten from
memory on the next sync.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the extra:

       pip install cognee-community-connector-stack-overflow

2. (Optional) Register a Stack Apps application at
   https://stackapps.com/apps/oauth/register
   to obtain a ``key``.  Without it the public API allows 300 requests/day;
   with a key the quota rises to 10 000.

3. Export the key (optional):

       export STACKOVERFLOW_API_KEY="your_key_here"

4. Set your LLM key (``LLM_API_KEY``) in ``.env`` like any other cognee example.

Run it:

    python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_stack_overflow import stack_overflow_source

DATASET_NAME = "stackoverflow_python"

SO_REMEMBER_KWARGS = {
    "primary_key": "question_id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
}


async def main():
    api_key = os.environ.get("STACKOVERFLOW_API_KEY")

    # Start from a clean slate so the demo is reproducible.
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)

    print("=== Initial sync (full backfill) ===")
    await cognee.remember(
        stack_overflow_source(
            tags=["python", "dlt"],
            api_key=api_key,
            include_answers=True,
        ),
        dataset_name=DATASET_NAME,
        **SO_REMEMBER_KWARGS,
    )

    answer = await cognee.search(
        query_text="How do I use dlt with Python to ingest data?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("Recall:", answer)

    print("\n=== Incremental re-sync (only changed/removed questions processed) ===")
    await cognee.remember(
        stack_overflow_source(
            tags=["python", "dlt"],
            api_key=api_key,
            include_answers=True,
        ),
        dataset_name=DATASET_NAME,
        **SO_REMEMBER_KWARGS,
    )
    print("Done — only changed questions were re-processed.")


if __name__ == "__main__":
    asyncio.run(main())
