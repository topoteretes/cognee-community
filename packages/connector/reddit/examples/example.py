"""Reddit connector demo: turn subreddit discussions into memory.

``reddit_source`` returns a ``dlt`` source you hand straight to ``cognee.remember``.
Every post is ingested with its comment thread as a normal document, in the node set
of its subreddit, so it goes through cognify entity extraction.

Re-running syncs only what changed: new posts are added, recent posts whose text or
comments changed are processed again, and removed or deleted posts are forgotten.

Privacy / opt-in
----------------
This reads the public posts and comments of the subreddits you choose, with their
authors' usernames. Nothing is fetched until you run this script. Keep Reddit in its
own dataset.

One-time setup
--------------
Create a "script" app at https://www.reddit.com/prefs/apps (Reddit approves Data API
access first), then export its client id and secret, a descriptive user agent, the
subreddits to sync and your LLM key, and run:

    export REDDIT_CLIENT_ID="..."
    export REDDIT_CLIENT_SECRET="..."
    export REDDIT_USER_AGENT="script:cognee-reddit:0.1 (by /u/your_name)"
    export REDDIT_SUBREDDITS="python,learnpython"
    export LLM_API_KEY="sk-..."
    uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_reddit import reddit_source

DATASET_NAME = "reddit"


async def main() -> None:
    needed = ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USER_AGENT", "REDDIT_SUBREDDITS")
    if not all(os.getenv(name) for name in needed):
        print(f"Set {', '.join(needed)} to run this example.")
        return

    source = reddit_source(
        subreddits=[s.strip() for s in os.environ["REDDIT_SUBREDDITS"].split(",") if s.strip()],
        client_id=os.environ["REDDIT_CLIENT_ID"],
        client_secret=os.environ["REDDIT_CLIENT_SECRET"],
        user_agent=os.environ["REDDIT_USER_AGENT"],
    )

    print("Syncing Reddit into cognee ...")
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
        self_improvement=False,
    )
    print("Sync stats:", source.cognee_sync_stats)

    results = await cognee.recall(
        "What do people recommend in the latest threads?", datasets=[DATASET_NAME]
    )
    print("\nRecall result:\n", results)

    print("\nRe-run later: only new and changed posts are synced, and removed posts are forgotten.")


if __name__ == "__main__":
    asyncio.run(main())
