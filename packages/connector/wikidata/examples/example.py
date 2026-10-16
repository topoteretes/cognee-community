"""Wikidata connector demo: turn Wikidata items into memory, "ask Wikidata".

Pulls Douglas Adams and his notable works from Wikidata into cognee and asks a
question about them. ``wikidata_source`` returns a ``dlt`` source you hand
straight to ``cognee.remember``; every item becomes a readable document (label,
description, statements with resolved labels and dates) that goes through
cognee's normal entity extraction.

Run it again and only items whose revision changed since the last run are
re-ingested; items deleted or merged on Wikidata are forgotten.

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
Wikidata is public and CC0; no account is needed. Nothing is fetched until you
run this script. Keep the items in their own dataset so you can drop them with
``cognee.forget``.

────────────────────────────────────────────────────────────────────────────
Run
────────────────────────────────────────────────────────────────────────────
    export LLM_API_KEY="sk-..."
    # Wikimedia asks API clients to identify themselves with contact details:
    # export WIKIDATA_USER_AGENT="my-app/1.0 (me@example.com)"
    uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_wikidata import wikidata_source

# Keep the items in their own dataset so they are easy to inspect and forget.
DATASET_NAME = "wikidata"


async def main() -> None:
    if not os.environ.get("LLM_API_KEY"):
        print("Set LLM_API_KEY to run this example.")
        return

    # Douglas Adams (Q42) plus his notable works (P800), one hop out.
    # sparql="SELECT ?item WHERE { ... } LIMIT 100" selects items by query instead.
    source = wikidata_source(
        items=["Q42"],
        expand_properties=["P800"],
        user_agent=os.environ.get(
            "WIKIDATA_USER_AGENT",
            "cognee-wikidata-example/0.1 (https://github.com/topoteretes/cognee-community)",
        ),
    )

    print("Syncing items from Wikidata into cognee ...")
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",  # required: incremental upsert by item id
        max_rows_per_table=0,  # read back every synced item, not just 50
    )
    stats = source.cognee_sync_stats
    print(
        f"{stats['mode']} sync: {stats['items_changed']} item(s) ingested, "
        f"{stats['items_unchanged']} unchanged, {stats['deleted']} forgotten."
    )

    results = await cognee.recall(
        "Which works is Douglas Adams known for, and when was he born?",
        datasets=[DATASET_NAME],
    )
    print("\nAnswer:", results[0].text if results else "(nothing found)")

    print(
        "\nRun this again later: only items edited on Wikidata since this run are "
        "re-ingested, and deleted or merged items are forgotten."
    )


if __name__ == "__main__":
    asyncio.run(main())
