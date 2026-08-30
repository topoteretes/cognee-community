"""Hacker News connector demo — turn tracked topics into memory.

Pull public HN stories and comments into cognee, with forget-on-delete.
``hacker_news_source`` returns a ``dlt`` source you hand straight to
``cognee.remember``. No API key.

Each run is a full snapshot: unchanged items keep a stable objectID and are
not re-cognified; items that disappear from Algolia search drop out of the
snapshot so cognee's orphan cleanup forgets them on the next sync.
"""

import asyncio
import os

import cognee

from cognee_community_connector_hacker_news import hacker_news_source

DATASET_NAME = "hacker_news"
DEFAULT_QUERIES = ["llm"]


async def main() -> None:
    queries = [
        q.strip() for q in os.environ.get("HN_QUERIES", "").split(",") if q.strip()
    ]
    if not queries:
        queries = DEFAULT_QUERIES
        print(f"HN_QUERIES unset; using {queries}")

    source = hacker_news_source(queries, max_pages=2)

    print("Syncing Hacker News into cognee ...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    answer = await cognee.search(
        query_text="Summarize the latest discussion.",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)
    print(
        "\nIf a story is deleted on HN, re-run: missing items are reconciled "
        "out of memory."
    )


if __name__ == "__main__":
    asyncio.run(main())
