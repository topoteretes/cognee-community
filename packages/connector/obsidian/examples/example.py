"""Obsidian connector demo — turn your vault into memory.

Sync a local Obsidian vault into cognee, incrementally and with
forget-on-delete. ``obsidian_source`` returns a ``dlt`` resource you hand
straight to ``cognee.remember``. Notes are ingested as normal documents (so
they go through the full cognify entity-extraction pipeline), with YAML
frontmatter as metadata and ``[[wikilinks]]`` resolved vault-wide into
note→note edges.

Re-running is cheap: the per-file (mtime, content-hash) cursor re-emits only
notes that actually changed, and notes you delete in the vault are forgotten
from memory on the next sync. A rename is a delete + create (Obsidian has no
stable note id — path is identity).

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This reads the content of your local vault. It is strictly opt-in — nothing is
read until you run this script. ``.obsidian/``, ``.trash/`` and
``*.sync-conflict-*`` files are excluded by default; add ``exclude=[...]``
glob patterns for anything else, and use a dedicated dataset so you can wipe
it with a single ``cognee.prune``.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the package:

       cd packages/connector/obsidian && uv sync

2. Point the connector at your vault and set your LLM key, then run:

       export OBSIDIAN_VAULT_PATH="$HOME/Documents/MyVault"
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py

Re-run after editing or deleting a note to see the incremental re-sync and
forget-on-delete.
"""

import asyncio
import os

import cognee

from cognee_community_connector_obsidian import obsidian_source

# Keep the vault in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "obsidian"


async def main() -> None:
    vault_path = os.environ.get("OBSIDIAN_VAULT_PATH")
    if not vault_path:
        print("Set OBSIDIAN_VAULT_PATH to your vault directory to run this example.")
        return

    source = obsidian_source(vault_path)  # exclude=["drafts/*"] to skip more

    print(f"Syncing Obsidian vault {vault_path!r} into cognee ...")
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",  # incremental upsert by note path
        max_rows_per_table=0,  # 0 = no row cap (vaults often exceed the default 50)
    )

    answer = await cognee.search(
        query_text="Summarize what these notes are about and how they link together.",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nEdit or delete a note in the vault, then re-run: only changed notes "
        "re-sync, and deleted notes are reconciled out of memory."
    )


if __name__ == "__main__":
    asyncio.run(main())
