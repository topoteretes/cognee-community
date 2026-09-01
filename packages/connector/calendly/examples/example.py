"""Calendly connector demo — "ask my calendar".

Pull Calendly scheduled events (+ invitee Q&A/notes) into cognee memory,
incrementally, with forget-on-delete.

This example is built on cognee's DLT ingestion subsystem: ``calendly_source``
returns a ``dlt`` resource that you hand straight to ``cognee.remember``. The
first run backfills recent + upcoming events; re-running ``remember`` narrows
the sync window (the ``min_start_time`` cursor) so only new/changed events are
re-fetched, and events you cancel in Calendly are forgotten from memory on the
next sync.

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This reads the content of your scheduled meetings, including whatever your
invitees wrote in their booking questions/notes. It is strictly opt-in —
nothing is fetched until you run this script. Use a dedicated dataset so you
can wipe it with a single ``cognee.forget``.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the extra:

       pip install "cognee[calendly]"     # or: uv sync --extra calendly

2. Create a Personal Access Token at
   https://calendly.com/integrations/api_webhooks (Integrations & apps ->
   API and webhooks).

3. Set your token and LLM key:

       export CALENDLY_API_TOKEN="..."
       export LLM_API_KEY="sk-..."

Run it:

    uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_calendly import calendly_source

# Keep the calendar in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "calendly"

# Routing kwargs shared by every remember() call below.
#   write_disposition="merge" is REQUIRED: the add pipeline defaults to
#     "replace", which would wipe the whole synced calendar on the second sync.
#   max_rows_per_table=0 disables cognee's per-table read cap so orphan-cleanup
#     (forget-on-delete) compares against the *entire* synced calendar, not a
#     50-row window.
CALENDLY_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
}


async def main() -> None:
    if not os.environ.get("CALENDLY_API_TOKEN"):
        print("Set CALENDLY_API_TOKEN to run this example.")
        return

    # Start from a clean slate so the demo is reproducible.
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)

    # ── First sync: backfill recent + upcoming events ──────────────────────
    print("\n=== Calendly sync #1 (backfill) ===")
    source = calendly_source(lookback_days=14)
    result = await cognee.remember(source, dataset_name=DATASET_NAME, **CALENDLY_REMEMBER_KWARGS)
    print(result)

    answer = await cognee.search(
        query_text="What are people expecting from our upcoming meetings?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("Calendar summary:", answer)

    # ── Second sync: incremental delta + forget-on-delete ───────────────────
    # Re-running with the SAME dataset reuses the persisted min_start_time
    # cursor: only the recent/future window is re-checked, and any event you
    # canceled in Calendly since sync #1 is removed from memory.
    print("\n=== Calendly sync #2 (incremental) ===")
    source = calendly_source()
    result = await cognee.remember(source, dataset_name=DATASET_NAME, **CALENDLY_REMEMBER_KWARGS)
    print(result)

    answer = await cognee.search(
        query_text="What changed on my calendar recently?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("Recent changes:", answer)


if __name__ == "__main__":
    asyncio.run(main())
