"""Browser history & bookmarks connector demo — "ask my research trail".

Pull your local browser history and bookmarks into cognee memory, incrementally,
with privacy-first defaults and forget-on-delete.

This example uses cognee's DLT ingestion subsystem: ``browser_source`` returns
a ``dlt`` resource that you pass directly to ``cognee.remember``. The first run
backfills your selected history and bookmarks; re-running ``remember`` syncs only
pages visited since the last run (using a high-precision timestamp cursor), and
pages deleted in the browser are forgotten from memory on the next sync.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install dependencies:

       pip install "cognee-community-connector-browser"
       # or from this monorepo:
       cd packages/connector/browser && uv sync --all-extras

2. Zero authentication is required:
   The connector reads your local browser profile directly from disk.
   No API keys, credentials, or tokens are needed.

3. Close or keep your browser open:
   The connector safely copies the profile's SQLite database and WAL journal
   to a temporary directory before querying, so it works even while Chrome, Edge,
   Brave, Vivaldi, or Firefox is actively running.

4. Set your LLM key (``LLM_API_KEY``) in ``.env`` like any other cognee run.

Run it:

    uv run python examples/example.py
"""

import asyncio
from datetime import datetime, timedelta, timezone

import cognee

from cognee_community_connector_browser import browser_source, detect_browser_profile

# Keep the browser history in its own dataset so it is easy to inspect and manage.
DATASET_NAME = "my_browser_trail"

# Routing kwargs shared by every remember() call.
# `max_rows_per_table=0` disables cognee's per-table read cap so orphan-cleanup
# (forget-on-delete) reconciles against the entire synced corpus.
BROWSER_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
}


async def main():
    # ── 1. Profile Detection ──────────────────────────────────────────────
    detected = detect_browser_profile()
    if detected:
        browser_type, profile_path = detected
        print(f"Auto-detected browser: {browser_type.title()} at {profile_path}")
    else:
        print("No supported browser profile automatically found on standard OS paths.")
        print("You can explicitly pass `browser='chrome'` and `profile_path='/path/to/profile'`.")
        return

    # Start from a clean slate so this demo run is completely reproducible.
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)

    # ── Example 1: Scoped Sync (e.g. Python & Rust docs from the last 30 days) ──
    # History can be huge; you can scope syncs using `since`, `include_domains`,
    # `min_visits`, or `bookmarks_only`.
    thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)

    print("\n=== Browser sync #1 (Scoped backfill: docs.python.org & doc.rust-lang.org) ===")
    source = browser_source(
        browser=browser_type,
        profile_path=profile_path,
        since=thirty_days_ago,
        include_domains=["docs.python.org", "doc.rust-lang.org"],
        min_visits=1,
        # fetch_content is False by default: privacy-first, ingesting title and metadata.
        # Set fetch_content=True to fetch readable page body text for deep semantic search.
        fetch_content=False,
    )

    result = await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        **BROWSER_REMEMBER_KWARGS,
    )
    print("Ingestion result:", result)

    # ── Querying Memory ──────────────────────────────────────────────────
    print("\n=== Querying research trail ===")
    answer = await cognee.search(
        query_text="What documentation pages did I read about async or iterators?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("Answer:", answer)

    # ── Example 2: Bookmarks-Only Sync ────────────────────────────────────
    print("\n=== Browser sync #2 (Bookmarks-only sync) ===")
    bookmarks_source = browser_source(
        browser=browser_type,
        profile_path=profile_path,
        bookmarks_only=True,
    )

    result_bookmarks = await cognee.remember(
        bookmarks_source,
        dataset_name="my_bookmarks",
        **BROWSER_REMEMBER_KWARGS,
    )
    print("Bookmarks ingestion result:", result_bookmarks)

    # ── Example 3: Incremental Sync + Forget-on-Delete ────────────────────
    # Re-running with the SAME dataset reuses the persisted last_visit_time cursor:
    # only URLs visited since the first run are read, and any history cleared or
    # deleted in the browser is emitted as tombstones and purged from cognee memory.
    print("\n=== Browser sync #3 (Incremental sync) ===")
    incremental_source = browser_source(
        browser=browser_type,
        profile_path=profile_path,
        include_domains=["docs.python.org", "doc.rust-lang.org"],
    )

    result_incremental = await cognee.remember(
        incremental_source,
        dataset_name=DATASET_NAME,
        **BROWSER_REMEMBER_KWARGS,
    )
    print("Incremental sync result:", result_incremental)


if __name__ == "__main__":
    asyncio.run(main())
