"""Shortcut connector demo: turn your Shortcut workspace into memory.

Pulls stories (with their comments), epics and iterations into cognee,
incrementally and with forget-on-delete. ``shortcut_source`` returns a ``dlt``
source you hand to ``cognee.remember``. Rows are ingested as normal documents,
so they go through the full cognify entity-extraction pipeline.

Run it twice. The first run ingests everything in the selection. Before the
second run, edit a story, add a comment, rename an epic or delete a story in
Shortcut: only the affected stories are fetched again, and the deleted one is
forgotten.

Privacy / opt-in
----------------
This reads the content of your Shortcut stories. Nothing is fetched until you
run this script. Scope what you ingest with ``SHORTCUT_EPIC_IDS`` or
``SHORTCUT_GROUP_IDS``, and use a dedicated dataset so you can wipe it with a
single ``cognee.forget``.

One-time setup
--------------
1. Install the package:

       uv pip install cognee-community-connector-shortcut

2. Create an API token in Shortcut under Settings > API Tokens. A read-only
   token is enough.
3. Export the token and your LLM key, then run:

       export SHORTCUT_API_TOKEN="..."
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py

   Optional, comma-separated: ``SHORTCUT_EPIC_IDS="16,42"`` (the number in an
   epic's URL) and ``SHORTCUT_GROUP_IDS="<team uuid>"``. Without them the whole
   workspace is ingested.
"""

import asyncio
import os

import cognee

from cognee_community_connector_shortcut import shortcut_source

# Keep Shortcut in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "shortcut"


def _ids(name: str) -> list[str]:
    return [part.strip() for part in os.environ.get(name, "").split(",") if part.strip()]


async def main() -> None:
    if not os.environ.get("SHORTCUT_API_TOKEN"):
        print("Set SHORTCUT_API_TOKEN to run this.")
        return

    epic_ids = [int(epic_id) for epic_id in _ids("SHORTCUT_EPIC_IDS")]
    group_ids = _ids("SHORTCUT_GROUP_IDS")
    scope = f"{len(epic_ids)} epic(s), {len(group_ids)} team(s)" if epic_ids or group_ids else "all"
    print(f"Syncing Shortcut into cognee (selection: {scope}) ...")
    await cognee.remember(
        shortcut_source(epic_ids=epic_ids or None, group_ids=group_ids or None),
        dataset_name=DATASET_NAME,
        primary_key="id",
        # Required: the default "replace" would drop every story that is not
        # re-emitted on an incremental run.
        write_disposition="merge",
        # 0 = no row cap, so orphan cleanup compares against the whole corpus.
        max_rows_per_table=0,
    )

    answer = await cognee.search(
        query_text="What is this team working on, and what is blocked or still open?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nEdit a story, add a comment, rename an epic or delete a story in Shortcut, then "
        "re-run: only the changes are synced and deleted stories are removed from memory."
    )


if __name__ == "__main__":
    asyncio.run(main())
