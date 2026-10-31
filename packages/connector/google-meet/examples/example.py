"""Sync Google Meet into cognee memory, incrementally.

Conferences you organized (who joined and when) and their transcripts are
turned into documents and cognified.  Re-running this script only processes
what is new, and transcripts whose Google Doc was deleted are forgotten.

Meet knows who joined, but not what the meeting was called: that lives in
Google Calendar.  With --with-calendar the script also syncs your calendar
into the same dataset (needs cognee-community-connector-google-calendar), so
a question like "who actually joined the Falcon drone sync and when?" can be
answered.  The two connectors write the same "Meeting link" line, which is
what links a conference to its event.

Setup (see the README for details):
  1. In Google Cloud: enable the Google Meet REST API and the Google Drive API,
     and create an OAuth client of type "Desktop app"; download its JSON.
  2. Set:
       GOOGLE_MEET_CREDENTIALS_PATH=~/client_secret.json
       GOOGLE_MEET_TOKEN_PATH=~/.cognee_google_meet_token.json
     plus the usual cognee LLM_API_KEY.  For --with-calendar, also the
     GOOGLE_CALENDAR_* variables from that connector's README.
  3. Sign in once:  python examples/login.py

Run:
    python examples/example.py [--with-calendar] ["optional question"]
"""

import asyncio
import sys

import cognee

from cognee_community_connector_google_meet import google_meet_source

DATASET = "google_meet_demo"


async def remember(source):
    await cognee.remember(
        source,
        dataset_name=DATASET,
        primary_key="id",
        # "merge" is required: it makes re-runs incremental and makes
        # deletions propagate through cognee's orphan cleanup.
        write_disposition="merge",
        # The default caps a table at 50 rows; meetings easily exceed it.
        max_rows_per_table=0,
    )
    # Counts only, never names or content.
    print("Sync stats:", source.cognee_sync_stats)


async def main():
    args = sys.argv[1:]
    with_calendar = "--with-calendar" in args
    args = [a for a in args if a != "--with-calendar"]

    print("=== Sync Google Meet ===")
    await remember(google_meet_source())  # reads GOOGLE_MEET_* env vars

    if with_calendar:
        from cognee_community_connector_google_calendar import google_calendar_source

        print("=== Sync Google Calendar ===")
        await remember(google_calendar_source())  # reads GOOGLE_CALENDAR_* env vars

    question = args[0] if args else "Who joined my recent meetings, and when?"
    answer = await cognee.recall(question, datasets=[DATASET])
    print("Recall:", answer)


if __name__ == "__main__":
    asyncio.run(main())
