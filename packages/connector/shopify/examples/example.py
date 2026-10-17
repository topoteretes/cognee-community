"""Shopify connector demo: turn your store's catalogue into memory.

Pulls products and collections (and, opt-in, customers and orders) into cognee,
incrementally and with forget-on-delete. ``shopify_source`` returns a ``dlt``
source you hand to ``cognee.remember``. Rows are ingested as normal documents,
so they go through the full cognify entity-extraction pipeline.

Run it twice. The first run ingests the catalogue. Before the second run, edit
a product and delete another in the Shopify admin: only the edited product (and
the collections that list it) is fetched again, and the deleted one is forgotten.

Privacy / opt-in
----------------
By default only products and collections are read. Customers and orders hold
personal data and are read only when ``SHOPIFY_RESOURCES`` names them. Use a
dedicated dataset so you can wipe it with a single ``cognee.forget``.

One-time setup (see the README for the dev store steps)
-------------------------------------------------------
1. Install the package:

       uv pip install cognee-community-connector-shopify

2. Export the store and the app's credentials (or ``SHOPIFY_ACCESS_TOKEN``),
   plus your LLM key, then run:

       export SHOPIFY_SHOP="your-store"            # or your-store.myshopify.com
       export SHOPIFY_CLIENT_ID="..."
       export SHOPIFY_CLIENT_SECRET="..."
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py

   Set ``SHOPIFY_RESOURCES=products,collections,customers,orders`` to include
   customers and orders.
"""

import asyncio
import os

import cognee

from cognee_community_connector_shopify import shopify_source

# Keep the store in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "shopify"


async def main() -> None:
    if not os.environ.get("SHOPIFY_SHOP"):
        print("Set SHOPIFY_SHOP and SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET to run this.")
        return
    resources = [
        r.strip()
        for r in os.environ.get("SHOPIFY_RESOURCES", "products,collections").split(",")
        if r.strip()
    ]

    print(f"Syncing {', '.join(resources)} from Shopify into cognee ...")
    await cognee.remember(
        shopify_source(resources=resources),
        dataset_name=DATASET_NAME,
        primary_key="id",
        # Required: the default "replace" would drop every product that is not
        # re-emitted on an incremental run.
        write_disposition="merge",
        # 0 = no row cap, so orphan cleanup compares against the whole corpus.
        max_rows_per_table=0,
    )

    answer = await cognee.search(
        query_text="Which products does the store sell, and how are they grouped?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nEdit a product or delete one in the Shopify admin, then re-run: "
        "only the changes are synced and deleted products are removed from memory."
    )


if __name__ == "__main__":
    asyncio.run(main())
