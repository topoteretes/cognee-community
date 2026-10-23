"""Runnable example: ingest Stack Overflow Q&A into cognee.

Usage:
    export STACKOVERFLOW_API_KEY=your_key_here   # optional but recommended
    python stack_overflow_sync.py
"""

import asyncio
import cognee
from cognee_community_connector_stack_overflow import (
    plan_stack_overflow_sync,
    stack_overflow_source,
)


async def main():
    # 1. Estimate request cost BEFORE ingesting (no data is fetched here).
    print(plan_stack_overflow_sync(tags=["polars", "duckdb"], max_questions_per_tag=200))

    # 2. Ingest questions + answers for two tags.
    await cognee.remember(
        stack_overflow_source(
            tags=["polars", "duckdb"],
            max_questions_per_tag=200,
            min_score=5,
            min_answers=1,
        ),
        dataset_name="stackoverflow",
        primary_key="id",
        write_disposition="merge",
    )

    # 3. Search ingested content.
    results = await cognee.search(
        "How do I lazily scan a CSV in polars?",
        datasets=["stackoverflow"],
    )
    for r in results[:3]:
        print(r)


if __name__ == "__main__":
    asyncio.run(main())