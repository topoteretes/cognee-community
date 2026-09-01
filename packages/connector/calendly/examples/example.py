"""Calendly connector demo — turn your scheduled events into memory.

Pull Calendly scheduled events into cognee, with forget-on-delete. ``calendly_source``
returns a ``dlt`` resource you hand straight to ``cognee.remember`` with
``write_disposition="merge"``. Events are ingested as normal documents (so they go
through the full cognify entity-extraction pipeline), including each invitee's
questions & answers and the meeting notes.

Each run is incremental: the first run backfills all active events and records their
``updated_at`` timestamps; later runs re-fetch only events whose ``updated_at``
advanced (a meeting note added after an event ends is picked up), and events you cancel
in Calendly drop out via the ``_deleted`` hard-delete marker, so cognee's orphan
cleanup forgets them from memory on the next sync.

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This reads your scheduled events, invitee answers, and meeting notes. It is strictly
opt-in — nothing is fetched until you run this script. Keep your Personal Access Token
private, and use a dedicated dataset so you can wipe it with a single ``cognee.prune``.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the extra:

       pip install "cognee[calendly]"      # or: uv sync --extra calendly

2. Create a Personal Access Token at https://calendly.com/integrations/api_webhooks
   and copy it.
3. Export the token and your LLM key, then run:

       export CALENDLY_API_TOKEN="eyJraWQiOi..."
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py

Re-run after scheduling or canceling an event to see the incremental sync and
forget-on-delete.
"""

import asyncio
import os

import cognee

from cognee_community_connector_calendly import calendly_source

# Keep Calendly in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "calendly"


async def main() -> None:
    if not os.environ.get("CALENDLY_API_TOKEN"):
        print("Set CALENDLY_API_TOKEN to run this example.")
        return

    # Scope with invitee_email="someone@example.com" to sync only events for that
    # invitee; omit it to ingest every event your token can see.
    source = calendly_source()

    print("Syncing Calendly events into cognee ...")
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        write_disposition="merge",  # REQUIRED — incremental sync, never "replace"
        max_rows_per_table=0,       # no row cap so forget-on-delete reconciles the whole corpus
    )

    answer = await cognee.search(
        query_text="What did invitees say in their booking questions?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nSchedule or cancel an event in Calendly, then re-run: new/rescheduled "
        "events are re-synced and canceled events are forgotten from memory."
    )


if __name__ == "__main__":
    asyncio.run(main())
