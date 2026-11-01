"""WooCommerce connector demo: turn a store's catalogue, reviews and orders into memory.

Pulls products, approved product reviews and orders (with their customer-facing
notes) into cognee, incrementally and with forget-on-delete.
``woocommerce_source`` returns a ``dlt`` resource you hand to ``cognee.remember``.
Rows are ingested as normal documents, so they go through the full cognify
entity-extraction pipeline.

Run it twice. The first run ingests the store. Before the second run, edit a
product, add a review or an order note, or trash a product: only the changes are
synced, and the trashed product is forgotten.

Privacy / opt-in
----------------
Nothing is fetched until you run this script. Billing and shipping details,
emails and phone numbers are never stored; customers, private order notes and
reviewer names are each opt-in (see the README). Use a dedicated dataset so you
can wipe it with a single ``cognee.forget``.

One-time setup
--------------
1. Install the package:

       uv pip install cognee-community-connector-woocommerce

2. Create a REST API key with **Read** permission in WooCommerce > Settings >
   Advanced > REST API. No store at hand? ``examples/local_store`` starts one in
   Docker and prints a key.
3. Export the store, the key and your LLM key, then run:

       export WOOCOMMERCE_STORE_URL="https://shop.example.com"
       export WOOCOMMERCE_CONSUMER_KEY="ck_..."
       export WOOCOMMERCE_CONSUMER_SECRET="cs_..."
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_woocommerce import woocommerce_source

# Keep the store in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "woocommerce"


async def main() -> None:
    needed = ("WOOCOMMERCE_STORE_URL", "WOOCOMMERCE_CONSUMER_KEY", "WOOCOMMERCE_CONSUMER_SECRET")
    if not all(os.environ.get(name) for name in needed):
        print("Set " + ", ".join(needed) + " to run this.")
        return

    print(f"Syncing {os.environ['WOOCOMMERCE_STORE_URL']} into cognee ...")
    source = woocommerce_source()
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        # Required: the default "replace" would drop every record that is not
        # re-emitted on an incremental run.
        write_disposition="merge",
        # 0 = no row cap, so orphan cleanup compares against the whole corpus.
        max_rows_per_table=0,
    )
    # listed, rendered, unchanged, deleted, kept, failed
    print("Sync stats:", source.cognee_sync_stats)

    answer = await cognee.recall(
        "What do customers say about sizing, and which orders asked for gift wrapping?",
        datasets=[DATASET_NAME],
    )
    print("\nRecall result:\n", answer)

    print(
        "\nEdit a product, add a review or an order note, or trash a product, then re-run: "
        "only the changes are synced and trashed products are removed from memory."
    )


if __name__ == "__main__":
    asyncio.run(main())
