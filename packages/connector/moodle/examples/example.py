"""Moodle connector demo — "ask my course".

Pull a Moodle course's sections, pages, books, assignments, glossaries and
announcements into cognee memory, incrementally, with forget-on-delete.

This example is built on cognee's DLT ingestion subsystem: ``moodle_source`` returns
a ``dlt`` resource that you hand straight to ``cognee.remember``. The first run
backfills the courses; re-running ``remember`` re-processes only items whose content
changed, and items you hide or delete in Moodle are forgotten from memory on the next
sync.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the package:

       pip install cognee-community-connector-moodle

2. Get a web service token for your Moodle account. On most sites the built-in
   "Moodle mobile web service" is enabled and a user can create a token under
   Preferences → Security keys; otherwise an administrator issues one under
   Site administration → Server → Web services → Manage tokens. For a local
   test site, moodlehq/moodle-docker runs Moodle at http://localhost:8000.

3. Export the site address and the token (the token is only ever sent in POST
   bodies, never in a URL):

       export MOODLE_BASE_URL="https://moodle.example.edu"
       export MOODLE_TOKEN="…"

   Optionally restrict the sync to some courses:

       export MOODLE_COURSE_IDS="7,8"

4. Set your LLM key (``LLM_API_KEY``) in ``.env`` like any other cognee example.

Run it:

    python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_moodle import moodle_source

# keep the courses in their own dataset so they are easy to inspect and forget
DATASET_NAME = "moodle_courses"
# one fixed name per moodle account: it keys the sync state and the forget-on-delete scope
RESOURCE_NAME = "moodle_main"

# max_rows_per_table=0 lets orphan-cleanup compare against the whole synced corpus
MOODLE_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
}


def _course_ids() -> list[int] | None:
    raw = os.environ.get("MOODLE_COURSE_IDS", "").strip()
    return [int(part) for part in raw.split(",") if part.strip()] or None


async def sync() -> None:
    source = moodle_source(resource_name=RESOURCE_NAME, course_ids=_course_ids())
    result = await cognee.remember(source, dataset_name=DATASET_NAME, **MOODLE_REMEMBER_KWARGS)
    print(result)
    print("sync stats:", source.cognee_sync_stats)


async def main():
    if not (os.environ.get("MOODLE_BASE_URL") and os.environ.get("MOODLE_TOKEN")):
        print(
            "Set MOODLE_BASE_URL and MOODLE_TOKEN.\n"
            "See the setup steps in this file's docstring, then re-run."
        )
        return

    print("\n=== Moodle sync #1 (backfill) ===")
    await sync()

    answer = await cognee.search(
        query_text="What is due soon, and what did the latest announcement say?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("Course answer:", answer)

    # the same dataset reuses the persisted state: only changed items are processed,
    # and anything hidden or deleted in moodle is removed from memory
    print("\n=== Moodle sync #2 (incremental) ===")
    await sync()


if __name__ == "__main__":
    asyncio.run(main())
