"""Calendly connector demo — turn your Calendly events into memory.

Pull Calendly scheduled events into cognee, with forget-on-delete. ``calendly_source``
returns a ``dlt`` source you hand straight to ``cognee.remember`` — no routing kwargs
needed. Events are ingested as normal documents (so they go through the full cognify
entity-extraction pipeline, unlike the relational dlt connectors).

Each run is a full snapshot: unchanged events keep a stable id and are not re-cognified,
and events you delete in Calendly drop out of the snapshot, so cognee's orphan cleanup
forgets them from memory on the next sync. Incremental sync with ``min_start_time``
ensures only changed data since the last run is fetched.

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This reads the content of your Calendly events and invitee responses. It is strictly
opt-in — nothing is fetched until you run this script. Use a dedicated dataset so you
can wipe it with a single ``cognee.prune``.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the extra:

       pip install "cognee[calendly]"      # or: uv sync --extra calendly

2. Create a personal access token at https://calendly.com/integrations/api_tokens and
   copy it.
3. Export the token and your LLM key, then run:

       export CALENDLY_API_KEY="your_token_here"
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py

Re-run after creating new events or modifying event details to see the re-sync and
forget-on-delete behavior when events are deleted.
"""

import asyncio
import os

import cognee

from cognee_community_connector_calendly import calendly_source

# Keep Calendly in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "calendly"


async def main() -> None:
    if not os.environ.get("CALENDLY_API_KEY"):
        print(
            "Set CALENDLY_API_KEY (create a personal access token at "
            "https://calendly.com/integrations/api_tokens) to run this example."
        )
        return

    # Optionally, you can pass min_start_time to sync only events starting at or
    # after a specific ISO 8601 datetime. For example:
    # source = calendly_source(min_start_time="2024-01-01T00:00:00Z")
    # Without it, all active events are ingested.
    source = calendly_source()

    print("Syncing Calendly events into cognee ...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    answer = await cognee.search(
        query_text="Summarize my upcoming events and invitee responses.",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nCreate a new event or modify event details in Calendly, then re-run: "
        "edits re-sync and deleted events are reconciled out of memory."
    )


if __name__ == "__main__":
    asyncio.run(main())
