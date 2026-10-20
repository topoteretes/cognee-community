"""Obsidian connector demo — turn a vault of markdown notes into memory.

Builds a tiny demo vault in a temp dir (so this runs with no account and no
existing notes), syncs it into cognee with forget-on-delete, and asks a question
that follows a [[wikilink]] edge. ``obsidian_source`` returns a ``dlt`` source
you hand straight to ``cognee.remember`` — no routing kwargs needed. Notes are
ingested as normal documents (so they go through the full cognify
entity-extraction pipeline, unlike the relational dlt connectors).

Each run is a full snapshot: unchanged notes keep a stable id and are not
re-cognified, and notes you delete from the vault drop out of the snapshot, so
cognee's orphan cleanup forgets them from memory on the next sync.

Point ``obsidian_source`` at your own vault to ingest real notes:

    source = obsidian_source("/path/to/vault")  # or include=["projects/*.md"]

Requires ``LLM_API_KEY`` like any other cognee run.
"""

import asyncio
import os
import tempfile
from pathlib import Path

import cognee

from cognee_community_connector_obsidian import obsidian_source

# Keep the vault in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "obsidian"

_DEMO_NOTES = {
    "agents.md": """---
title: Agents
tags: [ai, memory]
---

Agents remember things across sessions via [[memory]].
""",
    "memory.md": """---
title: Memory
tags: ai
---

Memory stores [[agents|agent]] context. See also [[agents#recall]].
""",
}


async def main() -> None:
    if not os.environ.get("LLM_API_KEY"):
        print("Set LLM_API_KEY to run this example.")
        return

    with tempfile.TemporaryDirectory(prefix="obsidian_demo_") as vault_dir:
        for name, text in _DEMO_NOTES.items():
            Path(vault_dir, name).write_text(text, encoding="utf-8")

        print("Syncing demo vault into cognee ...")
        await cognee.remember(obsidian_source(vault_dir), dataset_name=DATASET_NAME)

    answer = await cognee.search(
        query_text="What do agents use memory for?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nEdit or delete a note in your own vault, then re-run against it: edits "
        "re-sync and deleted notes are reconciled out of memory."
    )


if __name__ == "__main__":
    asyncio.run(main())
