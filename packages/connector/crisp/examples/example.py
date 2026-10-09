"""Crisp connector demo — turn your Crisp conversations into memory.

Pull Crisp shared-inbox conversations into cognee, with forget-on-delete.
``crisp_source`` returns a ``dlt`` source you hand straight to
``cognee.remember`` — no routing kwargs needed. Each conversation is ingested as
one normal document (so it goes through the full cognify entity-extraction
pipeline, unlike the relational dlt connectors).

Each run is a full snapshot: unchanged conversations keep a stable id and are
not re-cognified, and conversations deleted/aged-out upstream drop out of the
snapshot, so cognee's orphan cleanup forgets them from memory on the next sync.
Pass ``since=<epoch>`` to only re-sync conversations updated since a watermark.

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This reads the content of your Crisp conversations. It is strictly opt-in —
nothing is fetched until you run this script. Use a dedicated dataset so you can
wipe it with a single ``cognee.prune``.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the connector:

       pip install cognee-community-connector-crisp

2. Create a Crisp plugin (Marketplace → Plugins → New Plugin → Private) and grab
   a Development token keypair (identifier + key), plus your ``website_id``.

3. Export the credentials and your LLM key, then run:

       export CRISP_IDENTIFIER="..."
       export CRISP_KEY="..."
       export CRISP_WEBSITE_ID="..."
       export LLM_API_KEY="sk-..."
       python examples/example.py

Re-run after closing or deleting a conversation to see the re-sync and
forget-on-delete.
"""

import asyncio
import os

import cognee

from cognee_community_connector_crisp import crisp_source

# Keep Crisp in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "crisp"


async def main() -> None:
    missing = [
        name
        for name in ("CRISP_IDENTIFIER", "CRISP_KEY", "CRISP_WEBSITE_ID")
        if not os.environ.get(name)
    ]
    if missing:
        print(f"Set {', '.join(missing)} to run this example.")
        return

    # since=<epoch-seconds> restricts the sync to conversations updated after a
    # watermark (incremental cursor); omit it to sync everything.
    source = crisp_source()

    print("Syncing Crisp conversations into cognee ...")
    await cognee.remember(source, dataset_name=DATASET_NAME, max_rows_per_table=0)

    answer = await cognee.search(
        query_text="What did customers ask about?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nClose or delete a conversation in Crisp, then re-run: edits re-sync and "
        "removed conversations are reconciled out of memory."
    )


if __name__ == "__main__":
    asyncio.run(main())
