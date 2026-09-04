"""Todoist connector demo — turn your todo list into memory.

Pull Todoist projects, tasks, and comments into cognee, incrementally and with
forget-on-delete. ``todoist_source`` returns a ``dlt`` resource you hand
straight to ``cognee.remember``. Rows are ingested as normal documents (so they
go through the full cognify entity-extraction pipeline).

The first run is a full backfill; every later run fetches only what changed
since the last sync (Todoist's ``sync_token``) and propagates deletions, so
tasks you complete-and-delete upstream disappear from memory on the next sync.

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This reads the content of your tasks and comments. It is strictly opt-in —
nothing is fetched until you run this script. Use a dedicated dataset so you
can wipe it with a single ``cognee.forget``.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the extra:

       pip install "cognee[todoist]"      # or: uv sync --extra todoist

2. Create an API token at https://app.todoist.com/app/settings/integrations
   (Settings → Integrations → Developer).
3. Export the token and your LLM key, then run:

       export TODOIST_API_TOKEN="..."
       export LLM_API_KEY="sk-..."
       uv run python packages/connector/todoist/examples/example.py

Re-run after completing/deleting a task to see the incremental re-sync and
forget-on-delete.
"""

import asyncio

import cognee

from cognee_community_connector_todoist import todoist_source


async def main():
    source = todoist_source()  # reads TODOIST_API_TOKEN

    print("=== Initial sync (full backfill) ===")
    result = await cognee.remember(
        source,
        dataset_name="todoist_demo",
        primary_key="id",
        # "merge" is required: it's what makes re-runs incremental and what
        # makes deletions propagate via orphan cleanup. The default
        # ("replace") would wipe the synced corpus on every run.
        write_disposition="merge",
        # The DLT ingestion default caps a table at 50 rows; real accounts
        # exceed that, so lift the cap.
        max_rows_per_table=0,
    )
    print(result)

    answer = await cognee.recall("What do I need to do this week?")
    print("Recall:", answer)

    print("\n=== Incremental re-sync (only changes/deletions are processed) ===")
    result = await cognee.remember(
        todoist_source(),
        dataset_name="todoist_demo",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
