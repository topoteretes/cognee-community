"""Reddit connector demo — turn subreddit threads into memory.

Sync submissions and their comment trees from the subreddits you pick into
cognee, incrementally via the listing ``before``/``after`` cursor.
``reddit_source`` returns a ``dlt`` resource you hand straight to
``cognee.remember``. Each submission is ingested as one normal document (so it
goes through the full cognify entity-extraction pipeline), with subreddit /
author / date / score context as metadata and folded into the text so the
relationships become graph edges.

Re-running is cheap: each run pages ``/r/<sub>/new?before=<newest seen>`` for
what is new, re-renders only the most recent submissions to catch edits and
new replies, and re-checks known ids through ``/api/info`` so anything deleted
upstream is forgotten on the next sync.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Create a **script** app at https://www.reddit.com/prefs/apps ("create
   another app..." → type *script* → any redirect uri, e.g.
   http://localhost:8080). The id under the app name is the client id; the
   field labelled *secret* is the client secret.

2. Export the credentials (never hardcode them):

       export REDDIT_CLIENT_ID="..."
       export REDDIT_CLIENT_SECRET="..."
       export REDDIT_USERNAME="your-reddit-user"
       export REDDIT_PASSWORD="..."
       export REDDIT_USER_AGENT="python:my-cognee-bot:0.1.0 (by /u/your-reddit-user)"

3. Install and run:

       cd packages/connector/reddit && uv sync
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py

Re-run after new posts or edits to see the incremental re-sync; delete a post
upstream and re-run to see forget-on-delete.
"""

import asyncio
import os

import cognee

from cognee_community_connector_reddit import reddit_source

# Keep the subreddits in their own dataset so they are easy to inspect and forget.
DATASET_NAME = "reddit"

# Subreddits to ingest — "python", "r/Python" and "/r/python/" all work.
# Leave empty to fall back to the authenticated account's subscriptions.
SUBREDDITS: list[str] = ["r/LocalLLaMA"]


async def main() -> None:
    missing = [
        name for name in ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET") if not os.environ.get(name)
    ]
    if missing or not (
        os.environ.get("REDDIT_REFRESH_TOKEN")
        or (os.environ.get("REDDIT_USERNAME") and os.environ.get("REDDIT_PASSWORD"))
    ):
        print(
            "Set REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET and either "
            "REDDIT_USERNAME + REDDIT_PASSWORD or REDDIT_REFRESH_TOKEN to run this "
            "example. Create a script app at https://www.reddit.com/prefs/apps."
        )
        return

    source = reddit_source(
        SUBREDDITS,
        # Budgets for the comment-tree expansion — the issue's "one thread can
        # be thousands of calls" trap. 0 more-requests disables expansion.
        comment_depth=6,
        max_more_requests=5,
        # Keep the first run small so the demo finishes quickly.
        backfill_limit=25,
    )

    print(f"Syncing Reddit into cognee (subreddits: {SUBREDDITS or 'subscribed'}) ...")
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",  # incremental upsert by submission fullname
        max_rows_per_table=0,  # 0 = no row cap (busy subreddits exceed the default 50)
    )

    answer = await cognee.search(
        query_text="What are these subreddits arguing about, and who disagrees with whom?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nPost or edit something in a connected subreddit, then re-run: only "
        "new and changed submissions sync. Delete a post upstream and re-run to "
        "see it reconciled out of memory."
    )


if __name__ == "__main__":
    asyncio.run(main())
