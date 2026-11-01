"""RSS / Atom connector demo — turn your feeds into cognee memory.

Subscribe to blogs, changelogs, newsletters, and release feeds. ``rss_source``
returns a ``dlt`` source you hand straight to ``cognee.remember`` — no auth, no
routing kwargs. Entries are ingested as normal documents (so they go through
the full cognify entity-extraction pipeline).

Each run is incremental: only entries whose ``updated``/``published`` timestamp
is newer than the last sync are re-ingested, and entries that vanish from a
feed are reconciled out of memory (forget-on-delete).

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This fetches only the feed URLs you list. It is strictly opt-in — nothing is
fetched until you run this script — and entries go into a dedicated dataset so
you can wipe it with a single ``cognee.forget(dataset)``.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the connector:

       cd packages/connector/rss && uv sync

2. Export your LLM key (or rely on cognee's keyless local setup), then run:

       export LLM_API_KEY="sk-..."
       uv run python examples/example.py

Re-run after new posts appear to see the incremental sync; remove an entry
upstream and re-run to see forget-on-delete.
"""

import asyncio

import cognee

from cognee_community_connector_rss import rss_source

# Keep feeds in their own dataset so they are easy to inspect and forget.
DATASET_NAME = "feeds"

FEED_URLS = [
    # Swap in any feeds you follow — RSS 2.0, RSS 1.0/RDF, and Atom all work.
    "https://hnrss.org/frontpage",  # Hacker News front page
    "https://blog.python.org/feeds/posts/default",  # Python Insider (Atom)
]


async def main() -> None:
    print(f"Syncing {len(FEED_URLS)} feed(s) into cognee ...")
    await cognee.remember(
        rss_source(feed_urls=FEED_URLS),
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,  # unlimited read-back so deletions reconcile fully
    )

    answer = await cognee.search(
        query_text="What are the latest announcements in my feeds?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nRe-run after new posts appear: only new/updated entries are re-ingested, "
        "and entries removed upstream are reconciled out of memory."
    )


if __name__ == "__main__":
    asyncio.run(main())
