"""Freshsales connector demo - turn your CRM into memory.

Pull Freshsales contacts, accounts and deals (with their notes) into cognee,
incrementally and with forget-on-delete. ``freshsales_source`` returns a ``dlt``
source you hand to ``cognee.remember``.

First run: backfills everything. Later runs: only records updated since the last
run are fetched, and records you delete in Freshsales are forgotten.

Privacy: this reads customer data. Nothing is fetched until you run it. E-mail and
phone fields are left out by default.

Setup:

    cd packages/connector/freshsales && uv sync --all-extras
    export FRESHSALES_DOMAIN="acme"        # from https://acme.myfreshworks.com
    export FRESHSALES_API_KEY="..."        # Settings -> API Settings
    export LLM_API_KEY="sk-..."
    uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_freshsales import freshsales_source

DATASET_NAME = "freshsales"


async def main() -> None:
    if not (os.environ.get("FRESHSALES_DOMAIN") and os.environ.get("FRESHSALES_API_KEY")):
        print("Set FRESHSALES_DOMAIN and FRESHSALES_API_KEY to run this example.")
        return

    # Limit with entities=["deals"]; include_notes=False to skip notes.
    source = freshsales_source()

    print("Syncing Freshsales into cognee ...")
    # write_disposition="merge" is REQUIRED: incremental runs only see changes.
    await cognee.remember(source, dataset_name=DATASET_NAME, write_disposition="merge")

    answer = await cognee.search(
        query_text="Which deals are open, and what do the notes say about them?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print("\nEdit or delete a record in Freshsales, then re-run: only the changes are fetched.")


if __name__ == "__main__":
    asyncio.run(main())
