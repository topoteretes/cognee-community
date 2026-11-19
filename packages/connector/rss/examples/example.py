"""RSS / Atom connector demo — turn any feed into memory.

Pull feed entries into cognee, with forget-on-delete. ``rss_source`` returns a
``dlt`` source you hand straight to ``cognee.remember`` — no auth, no routing
kwargs. Entries are ingested as normal documents (so they go through the full
cognify entity-extraction pipeline, unlike the relational dlt connectors).

Each run is a full snapshot: unchanged entries keep a stable id and are not
re-cognified, and entries that drop out of the feed are reconciled out of memory
on the next sync.

────────────────────────────────────────────────────────────────────────────
Run
────────────────────────────────────────────────────────────────────────────
1. Install the connector:

       pip install "cognee-community-connector-rss"

2. Export your LLM key and one or more feed URLs, then run:

       export LLM_API_KEY="sk-..."
       export RSS_FEED_URLS="https://hnrss.org/frontpage,https://www.python.org/jobs/feed/rss/"
       uv run python examples/example.py

Re-run after the feed updates to see edits re-sync and dropped entries forgotten.
"""

import asyncio
import os

import cognee

from cognee_community_connector_rss import rss_source

# Keep RSS in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "rss"


async def main() -> None:
    if not os.environ.get("RSS_FEED_URLS"):
        print("Set RSS_FEED_URLS (comma-separated) to run this example.")
        return

    # Pass feed_urls=[...] explicitly, or rely on the RSS_FEED_URLS env var.
    source = rss_source()

    print("Syncing feed entries into cognee ...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    answer = await cognee.search(
        query_text="Summarize the main themes across these feed entries.",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nRe-run after the feed changes: edited entries re-sync and entries that "
        "dropped out of the feed are reconciled out of memory."
    )


if __name__ == "__main__":
    asyncio.run(main())
