"""Sync Outlook mail and calendar into cognee memory, incrementally.

Inbox and Sent Items plus the primary calendar are turned into documents and
cognified.  Re-running this script only processes what changed since the
last run, and deleted mail and events are forgotten automatically.

Setup (see the README for details):
  1. Register an app in Microsoft Entra ("Accounts in any organizational
     directory and personal Microsoft accounts", public client flows on),
     with the delegated permissions Mail.Read, Calendars.Read and User.Read.
  2. Set:
       OUTLOOK_CLIENT_ID=<application (client) id>
       OUTLOOK_TOKEN_CACHE_PATH=~/.cognee_outlook_token_cache.json
     plus the usual cognee LLM_API_KEY.
  3. Sign in once:  python examples/login.py

For a quick try without an app registration, set OUTLOOK_ACCESS_TOKEN to a
token from Graph Explorer instead (it expires after about an hour).

Run:
    python examples/example.py ["optional question about your mail and meetings"]
"""

import asyncio
import sys

import cognee

from cognee_community_connector_outlook import outlook_source

DATASET = "outlook_demo"


async def main():
    source = outlook_source()  # reads OUTLOOK_* env vars

    print("=== Sync ===")
    await cognee.remember(
        source,
        dataset_name=DATASET,
        primary_key="id",
        # "merge" is required: it makes re-runs incremental and makes
        # deletions propagate through cognee's orphan cleanup.
        write_disposition="merge",
        # The default caps a table at 50 rows; a mailbox easily exceeds it.
        max_rows_per_table=0,
    )
    # Counts only, never subjects or content.
    print("Sync stats:", source.cognee_sync_stats)

    question = sys.argv[1] if len(sys.argv) > 1 else "What are my recent emails and meetings about?"
    answer = await cognee.recall(question, datasets=[DATASET])
    print("Recall:", answer)


if __name__ == "__main__":
    asyncio.run(main())
