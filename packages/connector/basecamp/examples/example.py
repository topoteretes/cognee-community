"""Basecamp connector demo: sync a Basecamp account into cognee, then ask it questions.

Syncs messages, to-dos, documents and comments as normal cognee documents
(they go through the full cognify pipeline). Re-running only processes what
changed; items trashed in Basecamp are forgotten on the next sync, and items
purged from the trash are forgotten on the next full sweep.

Privacy / opt-in
----------------
This reads the content of your Basecamp projects. Nothing is fetched until you
run this script. Limit the scope with BASECAMP_PROJECT_IDS, and keep the data in
its own dataset so ``cognee.forget(dataset="basecamp")`` removes all of it.

Setup
-----
1. Get a token and your account id with ``examples/authorize.py``.
2. Export your settings and an LLM key, then run:

       export BASECAMP_ACCOUNT_ID="1234567"
       export BASECAMP_ACCESS_TOKEN="..."          # or BASECAMP_REFRESH_TOKEN +
       export BASECAMP_USER_AGENT="My Sync (me@example.com)"  # CLIENT_ID/SECRET
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py

Re-run after editing, completing or trashing something in Basecamp to see the
incremental sync and forget-on-delete.
"""

import asyncio
import os

import cognee

from cognee_community_connector_basecamp import basecamp_source

DATASET_NAME = "basecamp"


async def main() -> None:
    required = ("BASECAMP_ACCOUNT_ID", "BASECAMP_USER_AGENT")
    missing = [name for name in required if not os.environ.get(name)]
    if not (os.environ.get("BASECAMP_ACCESS_TOKEN") or os.environ.get("BASECAMP_REFRESH_TOKEN")):
        missing.append("BASECAMP_ACCESS_TOKEN (or BASECAMP_REFRESH_TOKEN)")
    if missing:
        print(f"Set {', '.join(missing)} to run this example.")
        return

    print("Syncing Basecamp into cognee ...")
    await cognee.remember(
        basecamp_source(),
        dataset_name=DATASET_NAME,
        # remember() defaults to "replace"; merge is what makes incremental sync
        # and the _deleted tombstones work.
        write_disposition="merge",
        # cognee reads only 50 rows per table by default.
        max_rows_per_table=0,
    )

    for question in (
        "Which to-dos are still open, and which are done?",
        "What did people say in the comments?",
    ):
        print(f"\nQ: {question}")
        for item in await cognee.recall(question, datasets=[DATASET_NAME]):
            print(f"A: {item}")


if __name__ == "__main__":
    asyncio.run(main())
