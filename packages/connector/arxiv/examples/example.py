"""arXiv connector demo — sync arXiv papers into memory.

Pull arXiv papers into cognee, with forget-on-delete. ``arxiv_source`` returns a
``dlt`` source you hand straight to ``cognee.remember`` — no routing kwargs
needed. Papers are ingested as normal documents (so they go through the full
cognify entity-extraction pipeline, unlike the relational dlt connectors).

Each run is a full snapshot: unchanged papers keep a stable id and are not
re-cognified, and papers that no longer match the query drop out of the
snapshot, so cognee's orphan cleanup forgets them from memory on the next sync.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the extra:

       pip install "cognee[arxiv]"

2. Export your LLM key, then run:

       export LLM_API_KEY="sk-..."
       uv run python examples/example.py

Re-run to see the re-sync and forget-on-delete.
"""

import asyncio
import os

import cognee

from cognee_community_connector_arxiv import arxiv_source

DATASET_NAME = "arxiv"


async def main() -> None:
    # Fetch recent AI papers (cs.AI category, last 7 days).
    source = arxiv_source(
        categories=["cs.AI", "cs.CL"],
        start_date="2026-08-23",
        max_results=20,
    )

    print("Syncing arXiv papers into cognee ...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    answer = await cognee.search(
        query_text="What are the latest trends in AI agent research?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nRe-run to sync newer papers. Papers that fall outside the date "
        "range on the next sync will be reconciled out of memory."
    )


if __name__ == "__main__":
    asyncio.run(main())