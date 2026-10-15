"""Readwise connector demo — turn your reading highlights into memory.

Syncs all your Readwise highlights and notes into cognee. Once ingested, an
AI agent can answer questions like:

    "What did I highlight about deep focus and flow states?"
    "Summarise my notes on Atomic Habits."
    "Which books have I highlighted the most?"
    "What are the key ideas I tagged as 'important'?"

How it works
────────────
Each book (or article, tweet, podcast) in your Readwise library becomes one
cognee document containing:
  * The title, author, category, and source URL
  * All your highlights in reading order
  * Any notes you added to each highlight
  * Tags on the book and individual highlights

Incremental sync
────────────────
Re-running this script only fetches books updated since the last run.
Highlights are grouped per book so cognee gets full context, not isolated
quotes.

Forget-on-delete
────────────────
Books you delete in Readwise are tombstoned on the next sync. Cognee's
orphan_cleanup removes them from the graph and vector stores.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the package:

       pip install cognee-community-connector-readwise

2. Get your Readwise access token:
   https://readwise.io/access_token

3. Export your token and LLM key:

       export READWISE_TOKEN="your-token-here"
       export LLM_API_KEY="sk-..."

4. Run:

       uv run python examples/example.py

Re-run to pick up new highlights — only changed books are re-fetched.
"""

import asyncio
import os

import cognee

from cognee_community_connector_readwise import readwise_source

# ── Configure these ──────────────────────────────────────────────────────────

# Optionally restrict to specific categories.
# Options: "books", "articles", "tweets", "supplementals", "podcasts"
# Set to None to ingest everything.
CATEGORIES = None  # e.g. ["books", "articles"]

# Keep Readwise in its own dataset.
DATASET_NAME = "readwise"

# ─────────────────────────────────────────────────────────────────────────────


async def main() -> None:
    token = os.environ.get("READWISE_TOKEN")
    if not token:
        print("Set READWISE_TOKEN to your Readwise access token.")
        print("Get it at: https://readwise.io/access_token")
        return

    source = readwise_source(
        token=token,
        categories=CATEGORIES,
    )

    print("Syncing Readwise highlights into cognee ...")
    await cognee.remember(source, dataset_name=DATASET_NAME)
    print("Sync complete.\n")

    # ── Example queries ──────────────────────────────────────────────────────

    queries = [
        "What are the most important ideas I highlighted about productivity?",
        "Summarise my highlights on habit formation.",
        "What books have I been reading recently?",
    ]

    for query in queries:
        print(f"Q: {query}")
        answer = await cognee.search(
            query_text=query,
            query_type=cognee.SearchType.GRAPH_COMPLETION,
            datasets=[DATASET_NAME],
        )
        print(f"A: {answer}\n")

    print(
        "Tip: add new highlights in Readwise, then re-run.\n"
        "Only the changed books will be re-fetched."
    )


if __name__ == "__main__":
    asyncio.run(main())
