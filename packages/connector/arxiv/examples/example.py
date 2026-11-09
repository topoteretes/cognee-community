"""arXiv connector demo — turn arXiv papers into memory.

Pull arXiv paper metadata and abstracts into cognee, with incremental sync.
``arxiv_source`` returns a ``dlt`` source you hand straight to
``cognee.remember`` — no routing kwargs needed. Papers are ingested as normal
documents (so they go through the full cognify entity-extraction pipeline).

Each run fetches papers matching your query. Use ``submitted_after`` for
incremental syncs to only pick up new papers since the last run.

One-time setup
--------------
1. Install the extra:

       pip install "cognee[arxiv]"      # or: uv sync --extra arxiv

2. No API key needed — arXiv's API is open.

3. Export your LLM key, then run:

       export LLM_API_KEY="sk-..."
       uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_arxiv import arxiv_source

DATASET_NAME = "arxiv"


async def main() -> None:
    # Fetch recent AI/ML papers from arXiv.
    source = arxiv_source(
        categories=["cs.AI", "cs.CL"],
        max_results=50,
        submitted_after="2026-01-01",
    )

    print("Syncing arXiv papers into cognee ...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    answer = await cognee.search(
        query_text="What are the main research themes in these papers?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nRe-run with a later submitted_after date to pick up only new papers "
        "(incremental sync)."
    )


if __name__ == "__main__":
    asyncio.run(main())
