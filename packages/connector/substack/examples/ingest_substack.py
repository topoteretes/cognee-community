"""Runnable example: ingest a public Substack newsletter into cognee and search it.

Run:
    cd packages/connector/substack
    pip install -e ".[dev]"
    python examples/ingest_substack.py

Set SUBSTACK_URL in your environment, or edit the DEFAULT_URL below.
"""

import asyncio
import os

import cognee
from cognee_community_connector_substack import substack_source

# ------------------------------------------------------------------
# Configuration — edit or set SUBSTACK_URL env var
# ------------------------------------------------------------------
DEFAULT_URL = "stratechery.com"  # public Substack-powered newsletter
SUBSTACK_URL = os.getenv("SUBSTACK_URL", DEFAULT_URL)
MAX_POSTS = int(os.getenv("MAX_POSTS", "5"))  # limit for demo speed
DATASET = "substack"


async def main() -> None:
    print(f"Ingesting up to {MAX_POSTS} posts from: {SUBSTACK_URL!r}")

    source = substack_source(SUBSTACK_URL, max_posts=MAX_POSTS)
    await cognee.remember(source, dataset_name=DATASET)

    print("\nIngestion complete. Running a sample search …\n")

    results = await cognee.search(
        query_text="What are the main topics covered in the newsletter?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET],
    )

    if not results:
        print("No results returned (the graph may still be processing).")
        return

    for i, result in enumerate(results, 1):
        print(f"[{i}] {result}")


if __name__ == "__main__":
    asyncio.run(main())
