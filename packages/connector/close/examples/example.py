"""Close CRM connector demo — turn your CRM data into memory.

Pull leads, opportunities, notes, and call summaries from Close into cognee,
with incremental sync and forget-on-delete.  ``close_source`` returns a ``dlt``
resource you hand straight to ``cognee.remember`` — each object becomes a
searchable document that flows through cognee's full entity-extraction pipeline.

Each run is incremental: unchanged objects keep a stable content hash and are
not re-ingested. Objects deleted in Close are removed from memory via
hard-delete tombstones on the next sync.

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This reads the content of your Close CRM data.  It is strictly opt-in — nothing
is fetched until you run this script.  Scope what you ingest with
``selected_types``, and use a dedicated dataset so you can wipe it with a single
``cognee.prune``.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the connector:

       pip install cognee-community-connector-close

2. Get your Close API key: Close → Settings → API Keys → create a new key
   (read permissions only).

3. Export the key and your LLM provider key, then run:

       export CLOSE_API_KEY="api_..."
       export LLM_API_KEY="sk-..."
       python examples/example.py

Re-run after editing or deleting data in Close to see incremental sync and
forget-on-delete in action.
"""

import asyncio
import os

import cognee

from cognee_community_connector_close import close_source

DATASET_NAME = "sales_memory"


async def main() -> None:
    if not os.environ.get("CLOSE_API_KEY"):
        print(
            "Set CLOSE_API_KEY (Settings → API Keys in Close) to run this example."
        )
        return

    # Select which object types to ingest. Default: lead, opportunity, note, call.
    # Add "email" to also ingest email threads.
    source = close_source(
        selected_types=["lead", "opportunity", "note", "call"],
    )

    print("Syncing Close CRM data into cognee ...")
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,  # unlimited — Close data can exceed the default 50
    )

    answer = await cognee.search(
        query_text="What objections came up in the latest call with Acme?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nEdit or delete data in Close, then re-run: edits re-sync and "
        "deleted records are forgotten from memory."
    )


if __name__ == "__main__":
    asyncio.run(main())