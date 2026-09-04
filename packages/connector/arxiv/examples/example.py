"""arXiv connector demo — turn paper metadata into memory.

Pull arXiv papers into cognee, with forget-on-delete. ``arxiv_source`` returns
a ``dlt`` source you hand straight to ``cognee.remember`` — no routing kwargs
needed. Papers are ingested as normal documents (so they flow through the full
cognify entity-extraction pipeline).

Each run is a full snapshot: unchanged papers keep a stable id and are not
re-cognified, and papers outside the query are reconciled out of memory on the
next sync.

────────────────────────────────────────────────────────────────────────────
Opt-in
────────────────────────────────────────────────────────────────────────────
This fetches public arXiv paper metadata and abstracts. Nothing is fetched
until you run this script. Scope what you ingest with ``categories`` /
``author`` / ``date_from`` / ``date_to``, and use a dedicated dataset so you
can wipe it with a single ``cognee.prune``.
"""

import asyncio

import cognee

from cognee_community_connector_arxiv import arxiv_source

# Keep arXiv in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "arxiv"


async def main() -> None:
    # Papers in the cs.AI / cs.LG categories from the last ~60 days.
    # Adjust the categories / date range to your interests.
    source = arxiv_source(
        categories=["cs.AI", "cs.LG"],
        max_results=200,
        date_from="2026-07-01",
    )

    print("Syncing arXiv papers into cognee ...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    answer = await cognee.search(
        query_text="What recent research directions are these papers exploring?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nAdjust the query (category / author / date range) and re-run: "
        "papers outside the new scope are reconciled out of memory."
    )


if __name__ == "__main__":
    asyncio.run(main())