"""Basecamp connector demo: sync a Basecamp account into cognee, then ask it questions.

Syncs messages, to-dos, documents and comments as normal cognee documents
(they go through the full cognify pipeline). Re-running only processes what
changed; items trashed in Basecamp are forgotten on the next sync, and items
purged from the trash are forgotten on the next full sweep.

Privacy / opt-in
----------------
This reads the content of your Basecamp projects. Nothing is fetched until you
run this script. Limit the scope with BASECAMP_PROJECT_IDS (comma separated),
and keep the data in
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

    # The source takes plain arguments; this example reads them from the environment.
    project_ids = os.environ.get("BASECAMP_PROJECT_IDS", "")
    source = basecamp_source(
        os.environ["BASECAMP_ACCOUNT_ID"],
        user_agent=os.environ["BASECAMP_USER_AGENT"],
        access_token=os.environ.get("BASECAMP_ACCESS_TOKEN"),
        refresh_token=os.environ.get("BASECAMP_REFRESH_TOKEN"),
        client_id=os.environ.get("BASECAMP_CLIENT_ID"),
        client_secret=os.environ.get("BASECAMP_CLIENT_SECRET"),
        project_ids=[p for p in project_ids.split(",") if p.strip()] or None,
    )

    print("Syncing Basecamp into cognee ...")
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        # remember() defaults to "replace"; merge is what makes incremental sync
        # and the _deleted tombstones work.
        write_disposition="merge",
        # 0 = no row cap, as in cognee's Notion example.
        max_rows_per_table=0,
    )

    for question in (
        "Which to-dos are still open, and which are done?",
        "What did people say in the comments?",
    ):
        print(f"\nQ: {question}")
        for item in await cognee.recall(question, datasets=[DATASET_NAME]):
            text = item.get("text") if isinstance(item, dict) else getattr(item, "text", item)
            print(f"A: {text}")


if __name__ == "__main__":
    asyncio.run(main())
