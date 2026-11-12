"""Attio connector demo — turn your Attio CRM into memory.

Pull Attio records (people, companies, deals and any custom object), their notes and,
optionally, list entries into cognee, with forget-on-delete. ``attio_source`` returns a
``dlt`` source you hand straight to ``cognee.remember``. Rows are ingested as normal
documents, so they go through the full cognify entity-extraction pipeline.

Each run is a full snapshot: unchanged rows keep a stable id and are not re-cognified, and
records you delete in Attio drop out of the snapshot, so cognee's orphan cleanup forgets them
from memory on the next sync.

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This reads the content of your CRM. It is strictly opt-in — nothing is fetched until you run
this script. Use a token with read-only scopes, scope what you ingest with ``objects`` /
``lists``, and use a dedicated dataset so you can wipe it with a single ``cognee.prune``.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the package:

       uv sync                 # from packages/connector/attio

2. In Attio open Workspace settings → Developers → "+ New access token" and grant it the
   read scopes: ``object_configuration:read``, ``record_permission:read`` and ``note:read``
   (plus ``list_configuration:read`` and ``list_entry:read`` if you ingest lists).
3. Export the token and your LLM key, then run:

       export ATTIO_API_KEY="..."
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py

Re-run after editing or deleting a record to see the re-sync and forget-on-delete.
"""

import asyncio
import os

import cognee

from cognee_community_connector_attio import attio_source

# Keep Attio in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "attio"


async def main() -> None:
    if not os.environ.get("ATTIO_API_KEY"):
        print("Set ATTIO_API_KEY (a read-only Attio access token) to run this example.")
        return

    # Scope with objects=["people", "companies"] and lists=["sales_pipeline"]; omit both to
    # ingest every object in the workspace (custom objects included) without list entries.
    source = attio_source()

    print("Syncing Attio into cognee ...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    answer = await cognee.search(
        query_text="Which companies do we have notes about, and what do the notes say?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nEdit or delete a record in Attio, then re-run: edits re-sync and "
        "deleted records are reconciled out of memory."
    )


if __name__ == "__main__":
    asyncio.run(main())
