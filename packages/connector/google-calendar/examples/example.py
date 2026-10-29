"""Sync Google Calendar into cognee memory, incrementally.

Events from the selected calendars are turned into documents and cognified.
Re-running this script only processes what changed since the last run, and
deleted events are forgotten automatically.

Setup (see the README for details):
  1. In Google Cloud: enable the Google Calendar API and create an OAuth
     client of type "Desktop app"; download its JSON.
  2. Set:
       GOOGLE_CALENDAR_CREDENTIALS_PATH=~/client_secret.json
       GOOGLE_CALENDAR_TOKEN_PATH=~/.cognee_google_calendar_token.json
       GOOGLE_CALENDAR_IDS=primary          (optional, comma-separated)
     plus the usual cognee LLM_API_KEY.
  3. Sign in once:  python examples/login.py

Run:
    python examples/example.py ["optional question about your calendar"]
"""

import asyncio
import sys

import cognee

from cognee_community_connector_google_calendar import google_calendar_source

DATASET = "google_calendar_demo"


async def main():
    source = google_calendar_source()  # reads GOOGLE_CALENDAR_* env vars

    print("=== Sync ===")
    await cognee.remember(
        source,
        dataset_name=DATASET,
        primary_key="id",
        # "merge" is required: it makes re-runs incremental and makes
        # deletions propagate through cognee's orphan cleanup.
        write_disposition="merge",
        # The default caps a table at 50 rows; a calendar easily exceeds it.
        max_rows_per_table=0,
    )
    # Counts only, never titles or content.
    print("Sync stats:", source.cognee_sync_stats)

    question = sys.argv[1] if len(sys.argv) > 1 else "What meetings are on my calendar?"
    answer = await cognee.recall(question, datasets=[DATASET])
    print("Recall:", answer)


if __name__ == "__main__":
    asyncio.run(main())
