"""Granola connector demo — turn your Granola meeting notes into memory.

Pull Granola meeting notes into cognee, with forget-on-delete. ``granola_source``
returns a ``dlt`` source you hand straight to ``cognee.remember`` — no routing
kwargs needed. Notes are ingested as normal documents (so they flow through the
full cognify entity-extraction pipeline, unlike relational dlt connectors).

Each run is a full snapshot: unchanged notes keep a stable id and are not
re-cognified. Notes deleted in Granola drop out of the snapshot, so cognee's
orphan cleanup forgets them from graph and vector memory on the next sync.

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This connector reads your Granola meeting notes, summaries, and transcripts.
During ``cognify``, note content is sent to your configured LLM for entity and
relationship extraction. It is strictly opt-in — nothing is fetched until you
run this script. Use a dedicated dataset name (e.g. ``"granola"``) so you can
inspect, query, or wipe it with a single ``cognee.prune``.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Granola Plan:
   An active Business or Enterprise plan is required to generate API keys.
   (On Enterprise plans, an admin must enable API access in Workspace settings).

2. Generate an API Key:
   In the Granola desktop app, navigate to:
   Settings -> Connectors -> API keys -> Create API key.
   - A Personal key accesses your notes and notes shared directly with you.
   - A Workspace key accesses all workspace notes.

3. Export your keys and run:

       export GRANOLA_API_KEY="grn_..."
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py

Re-run after editing or deleting a note to verify re-sync and forget-on-delete.
"""

import asyncio
import os

import cognee

from cognee_community_connector_granola import granola_source

# Keep Granola in its own dataset so it is easy to inspect and prune.
DATASET_NAME = "granola"


async def main() -> None:
    if not os.environ.get("GRANOLA_API_KEY"):
        print("Set GRANOLA_API_KEY in your environment to run this example.")
        return

    # Ingest Granola meeting notes into cognee.
    # Set include_transcript=False if you only wish to index AI summaries.
    source = granola_source(include_transcript=True)

    print("Syncing Granola notes into cognee ...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    answer = await cognee.search(
        query_text="What did I commit to in my meetings?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nEdit or delete a note in Granola, then re-run: edits re-sync and "
        "deleted notes are automatically reconciled out of memory."
    )


if __name__ == "__main__":
    asyncio.run(main())
