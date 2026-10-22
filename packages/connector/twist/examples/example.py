"""Twist connector demo: sync a Twist workspace into cognee, then ask it questions.

Each thread (opening post plus its comments, in order) becomes one cognee
document that goes through the full cognify pipeline, and each channel becomes
a short document of its own. Re-running only processes what changed; a thread
deleted in Twist is forgotten on the next sync.

Privacy / opt-in
----------------
This reads the threads of your Twist workspace. Nothing is fetched until you run
this script. Direct messages are never read. Limit the scope with
TWIST_CHANNEL_IDS, and keep the data in its own dataset so
``cognee.forget(dataset="twist")`` removes all of it.

Setup
-----
1. Get a token and your workspace id with ``examples/authorize.py`` (or use the
   integration's test token while developing).
2. Export your settings (an LLM key is optional), then run:

       export TWIST_WORKSPACE_ID="254427"
       export TWIST_ACCESS_TOKEN="..."
       export TWIST_CHANNEL_IDS="852087,852086"   # optional
       export LLM_API_KEY="sk-..."                # optional, see below
       uv run python examples/example.py

Without LLM_API_KEY, cognee runs in its keyless mode (local GLiNER extraction
and fastembed embeddings, downloaded on first use) and recall returns the
matching passages instead of a written answer.

Re-run after adding a comment, editing a thread or deleting one in Twist to see
the incremental sync and forget-on-delete.
"""

import asyncio
import os

import cognee

from cognee_community_connector_twist import twist_source

DATASET_NAME = "twist"


async def main() -> None:
    missing = [
        name for name in ("TWIST_WORKSPACE_ID", "TWIST_ACCESS_TOKEN") if not os.environ.get(name)
    ]
    if missing:
        print(f"Set {', '.join(missing)} to run this example.")
        return

    channels = [c.strip() for c in os.environ.get("TWIST_CHANNEL_IDS", "").split(",")]

    print("Syncing Twist into cognee ...")
    await cognee.remember(
        twist_source(
            os.environ["TWIST_WORKSPACE_ID"],
            access_token=os.environ["TWIST_ACCESS_TOKEN"],
            channel_ids=[int(c) for c in channels if c] or None,
        ),
        dataset_name=DATASET_NAME,
        # remember() defaults to "replace"; merge is what makes incremental sync
        # and the _deleted tombstones work.
        write_disposition="merge",
    )

    for question in (
        "What did the team decide about the checkout redesign?",
        "When is the launch?",
    ):
        print(f"\nQ: {question}")
        for item in await cognee.recall(question, datasets=[DATASET_NAME]):
            text = item.get("text") if isinstance(item, dict) else getattr(item, "text", item)
            print(f"A: {text}")


if __name__ == "__main__":
    asyncio.run(main())
