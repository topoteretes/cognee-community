"""Guru connector demo — give your AI agents your team's knowledge base.

Pull Guru cards into cognee, with forget-on-delete. ``guru_source`` returns a
``dlt`` source you hand straight to ``cognee.remember`` — no routing kwargs
needed. Cards are ingested as normal documents (so they go through the full
cognify entity-extraction pipeline, unlike the relational dlt connectors).

Each run is a full snapshot: unchanged cards keep a stable id and are not
re-cognified, and cards you archive or delete in Guru drop out of the snapshot,
so cognee's orphan cleanup forgets them from memory on the next sync.

Card verification state travels with the card text, so an agent can tell
trusted knowledge from something a human still needs to re-check.

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This reads the content of your Guru workspace, including cards that are private
to you. It is strictly opt-in — nothing is fetched until you run this script.
Scope what you ingest with ``folder_id`` / ``verification_state``, and use a
dedicated dataset so you can wipe it with a single ``cognee.prune``.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install this connector's dependencies:

       pip install -e .

2. Create an API token in Guru (Settings → API Tokens) and note the email the
   token belongs to.
3. Export the credentials and your LLM key, then run:

       export GURU_USER="you@example.com"
       export GURU_TOKEN="..."
       export LLM_API_KEY="sk-..."
       python examples/example.py

Re-run after editing or deleting a card in Guru to see the re-sync and
forget-on-delete.
"""

import asyncio
import os

import cognee

from cognee_community_connector_guru import guru_source

# Keep Guru in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "guru"


async def main() -> None:
    if not (os.environ.get("GURU_USER") and os.environ.get("GURU_TOKEN")):
        print("Set GURU_USER and GURU_TOKEN (plus LLM_API_KEY) to run this example.")
        return

    # Scope with folder_id="..." and/or verification_state="trusted"; omit both
    # to ingest every card the token can see.
    source = guru_source()

    print("Syncing Guru cards into cognee ...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    answer = await cognee.search(
        query_text="Summarize what these Guru cards are about.",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nEdit or delete a card in Guru, then re-run: edits re-sync and "
        "removed cards are reconciled out of memory."
    )


if __name__ == "__main__":
    asyncio.run(main())
