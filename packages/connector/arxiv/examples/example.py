"""arXiv connector demo — turn a slice of arXiv into memory.

Pull paper metadata and abstracts into cognee, with forget-on-delete.
``arxiv_source`` returns a ``dlt`` source you hand straight to
``cognee.remember`` — no routing kwargs needed. Papers are ingested as normal
documents (so they go through the full cognify entity-extraction pipeline,
unlike the relational dlt connectors).

Each run is a full snapshot *of your query*: unchanged papers keep a stable id
and are not re-cognified, and a paper that stops matching — withdrawn or
reclassified — drops out of the snapshot, so cognee's orphan cleanup forgets it
on the next sync.

────────────────────────────────────────────────────────────────────────────
Scope and etiquette
────────────────────────────────────────────────────────────────────────────
arXiv needs no account and no API key, but it is a free service run on a
shoulder-string budget: the connector spaces requests 3 seconds apart, per
arXiv's terms. Keep your query narrow — a category plus a date window, not
``all:*`` — and it stays a handful of requests.

A query is required. Syncing all of arXiv is refused rather than attempted.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the extra:

       pip install "cognee[arxiv]"      # or: uv sync --extra arxiv

2. Export your LLM key and run:

       export LLM_API_KEY="sk-..."
       uv run python examples/example.py

Widen or narrow the window below and re-run to see the re-sync and
forget-on-delete.
"""

import asyncio
import os

import cognee

from cognee_community_connector_arxiv import arxiv_source

# Keep arXiv in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "arxiv"


async def main() -> None:
    if not os.environ.get("LLM_API_KEY"):
        print("Set LLM_API_KEY to run this example (arXiv itself needs no key).")
        return

    # A narrow slice: recent cs.AI papers. Scope with categories=, authors=,
    # a submitted_from/submitted_to window, or a raw search_query=.
    source = arxiv_source(
        categories=["cs.AI"],
        submitted_from="20260101",
        max_results=25,
    )

    print("Syncing arXiv papers into cognee ...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    answer = await cognee.search(
        query_text="What problems are these papers trying to solve?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nNarrow or widen the query, then re-run: new matches sync in and "
        "papers that no longer match are reconciled out of memory."
    )


if __name__ == "__main__":
    asyncio.run(main())
