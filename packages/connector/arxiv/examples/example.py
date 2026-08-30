"""arXiv connector demo — turn a paper feed into memory.

Pull arXiv paper metadata and abstracts into cognee, incrementally and with
forget-on-delete. ``arxiv_source`` returns a ``dlt`` source you hand straight to
``cognee.remember``. Papers are ingested as normal documents, so each abstract
flows through the full cognify entity-extraction pipeline — the right treatment
for prose — and you can then ask questions across the corpus.

Re-running syncs only papers submitted since the last run: the ``submittedDate``
cursor lives in dlt's per-resource state, so the second run is cheap and papers
that left the corpus are reconciled out of memory.

────────────────────────────────────────────────────────────────────────────
No account needed
────────────────────────────────────────────────────────────────────────────
The arXiv Atom API is public and read-only — no key, no signup. It does ask for
roughly one request every three seconds, which the connector enforces for you.
That makes wall-clock time a function of corpus size, so this example stays
deliberately small (``max_papers``); see the README's cost table before pointing
it at an entire category.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the extra:

       pip install "cognee[arxiv]"      # or: uv sync --extra arxiv

2. Export your LLM key and run:

       export LLM_API_KEY="sk-..."
       uv run python examples/example.py

Run it twice to watch the incremental path: the second run re-syncs only what
was submitted in between.
"""

import asyncio

import cognee

from cognee_community_connector_arxiv import arxiv_source

# Keep arXiv in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "arxiv_papers"

# A narrow scope keeps the demo to a handful of requests. Widen it (drop
# max_papers, add categories) once you have seen the flow work.
CATEGORIES = ["cs.AI"]
MAX_PAPERS = 50


async def main() -> None:
    source = arxiv_source(
        categories=CATEGORIES,
        max_papers=MAX_PAPERS,
        # Revisions bump `updated`, not `published`, so the submittedDate cursor
        # alone would miss them. The deletion sweep already fetches the entries,
        # so switching this on costs no extra requests.
        track_revisions=True,
    )

    print(f"Syncing up to {MAX_PAPERS} {'/'.join(CATEGORIES)} papers into cognee ...")
    print("(arXiv is rate-limited to ~1 request / 3s, so this takes a moment.)")
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        # Required: the add pipeline defaults to "replace", which would wipe the
        # synced corpus on the second sync instead of upserting into it.
        write_disposition="merge",
        # 0 = unlimited, so orphan-cleanup compares against the whole corpus
        # rather than the default 50-row window.
        max_rows_per_table=0,
    )

    answer = await cognee.search(
        query_text="What problems do these papers address, and how do they relate?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nRe-run this script to see the incremental path: only papers submitted "
        "since this run are fetched, and papers that left the corpus are "
        "reconciled out of memory."
    )


if __name__ == "__main__":
    asyncio.run(main())
