"""Stripe connector demo — turn your billing data into memory.

Pull customers, subscriptions, invoices, charges, products and disputes from
Stripe into cognee, with incremental sync and forget-on-delete.
``stripe_source`` returns a ``dlt`` source you hand straight to
``cognee.remember``. Each object is ingested as a document that names its
related objects as node sets, so a customer, its subscriptions, their
invoices and the charges on them are linked in the graph.

The first run lists everything. Later runs read Stripe's events since the
previous run and re-read only the objects that changed; objects deleted in
Stripe are forgotten.

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This reads billing records of the Stripe account you point it at. Card and
payment-method details, addresses and receipt links are never ingested;
customer email and phone are left out unless you opt in. Nothing is fetched
until you run this script. Use a dedicated dataset so you can wipe it with a
single ``cognee.forget``.

────────────────────────────────────────────────────────────────────────────
One-time setup (see README.md for the click-by-click version)
────────────────────────────────────────────────────────────────────────────
1. Connect: either your Connect platform's secret key plus the connected
   account id from Stripe Connect OAuth (README.md → "Setup: Stripe Connect"),
   or, for a quick local try on your own account, a restricted key with read
   access to the objects below and no account id.
2. Export them with your LLM key, then run:

       export STRIPE_API_KEY="sk_test_..."         # or rk_test_... for a local try
       export STRIPE_ACCOUNT_ID="acct_..."         # omit for your own account
       export STRIPE_OBJECTS="customers,subscriptions,invoices"  # optional, default all
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py

Re-run after editing a customer, paying an invoice or deleting a product to
see the incremental re-sync and forget-on-delete.
"""

import asyncio
import os

import cognee

from cognee_community_connector_stripe import stripe_source

# Keep Stripe in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "stripe"


def _objects() -> list[str] | None:
    """Comma-separated object types from STRIPE_OBJECTS, or None for all."""
    names = [name.strip() for name in os.environ.get("STRIPE_OBJECTS", "").split(",")]
    return [name for name in names if name] or None


async def main() -> None:
    if not os.environ.get("STRIPE_API_KEY"):
        print("Set STRIPE_API_KEY (and STRIPE_ACCOUNT_ID for a connected account); see README.md.")
        return

    print("Syncing Stripe billing data into cognee ...")
    # write_disposition must be "merge": later runs emit only what changed, plus deletions.
    await cognee.remember(
        stripe_source(
            os.environ["STRIPE_API_KEY"],
            account_id=os.environ.get("STRIPE_ACCOUNT_ID"),
            objects=_objects(),
        ),
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",
        self_improvement=False,
    )

    answer = await cognee.search(
        query_text="Which customers have unpaid invoices or canceled subscriptions?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nEdit a customer, pay or void an invoice, or delete a product in Stripe, then "
        "re-run: changed objects re-sync and deleted ones are forgotten."
    )


if __name__ == "__main__":
    asyncio.run(main())
