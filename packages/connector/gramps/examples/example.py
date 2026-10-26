"""Gramps Web connector demo: "ask my family tree".

Pull the people and families of a Gramps Web tree into cognee memory, incrementally,
with forget-on-delete.

``gramps_source`` returns a ``dlt`` resource that you hand straight to
``cognee.remember``. The first run backfills the tree; re-running ``remember``
re-processes only the people and families whose document changed, and anyone you
delete in Gramps Web is forgotten on the next sync.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the package:

       pip install cognee-community-connector-gramps

2. Use a Gramps Web account that can read the tree (any role). To try it without a
   server of your own, the public demo at https://demo.grampsweb.org works with the
   demo account listed on grampsweb.org.

3. Export the address and the account:

       export GRAMPS_URL="https://gramps.example.org"
       export GRAMPS_USERNAME="…"
       export GRAMPS_PASSWORD="…"

4. Set your LLM key (``LLM_API_KEY``) in ``.env`` like any other cognee example.

Run it:

    python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_gramps import gramps_source

# keep the tree in its own dataset so it is easy to inspect, share and forget
DATASET_NAME = "family_tree"
# one fixed name per tree: it keys the sync state and the forget-on-delete scope
RESOURCE_NAME = "gramps_family_tree"

# max_rows_per_table=0 lets orphan-cleanup compare against the whole synced tree
GRAMPS_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
}


async def sync() -> None:
    source = gramps_source(resource_name=RESOURCE_NAME)  # reads the GRAMPS_* variables
    result = await cognee.remember(source, dataset_name=DATASET_NAME, **GRAMPS_REMEMBER_KWARGS)
    print(result)
    print("sync stats:", source.cognee_sync_stats)


async def main():
    missing = [
        name
        for name in ("GRAMPS_URL", "GRAMPS_USERNAME", "GRAMPS_PASSWORD")
        if not os.environ.get(name)
    ]
    if missing:
        print(
            f"Set {', '.join(missing)}.\nSee the setup steps in this file's docstring, then re-run."
        )
        return

    print("\n=== Gramps sync #1 (backfill) ===")
    await sync()

    answer = await cognee.recall(
        query_text=(
            "Who were the parents of the oldest person in the tree, and where were they from?"
        ),
        datasets=[DATASET_NAME],
    )
    print("Family tree answer:", answer)

    # the same dataset reuses the persisted state: only changed documents are processed,
    # and anyone deleted in Gramps Web is removed from memory
    print("\n=== Gramps sync #2 (incremental) ===")
    await sync()


if __name__ == "__main__":
    asyncio.run(main())
