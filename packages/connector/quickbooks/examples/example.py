"""QuickBooks Online connector demo — sync accounting transactions into Cognee memory.

Pull QuickBooks Online invoices, bills, and credit memos into Cognee memory with full
forget-on-delete support. `quickbooks_source` yields DLT resources suitable for passing
directly to `cognee.remember`.

────────────────────────────────────────────────────────────────────────────
QuickBooks Developer & Sandbox Setup
────────────────────────────────────────────────────────────────────────────
1. Sign in or create a developer account at https://developer.intuit.com
2. Create an App under Dashboard (select "Accounting" scope).
3. Access your Sandbox company under Dashboard -> Sandbox.
4. Obtain credentials via the Intuit OAuth 2.0 Playground:
   - Client ID & Client Secret (from App Keys)
   - Company realmId
   - Refresh Token (or active Access Token)

5. Export environment variables:
       export QUICKBOOKS_REALM_ID="1234567890"
       export QUICKBOOKS_ACCESS_TOKEN="ey..."
       # Or for auto-refresh:
       # export QUICKBOOKS_CLIENT_ID="AB..."
       # export QUICKBOOKS_CLIENT_SECRET="cd..."
       # export QUICKBOOKS_REFRESH_TOKEN="rt..."
       export LLM_API_KEY="sk-..."

6. Run this example:
       uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_quickbooks import quickbooks_source

DATASET_NAME = "quickbooks_accounting"


async def main() -> None:
    realm_id = os.environ.get("QUICKBOOKS_REALM_ID")
    access_token = os.environ.get("QUICKBOOKS_ACCESS_TOKEN")
    refresh_token = os.environ.get("QUICKBOOKS_REFRESH_TOKEN")

    if not realm_id or (not access_token and not refresh_token):
        print("Missing required QuickBooks credentials.")
        print("Please export QUICKBOOKS_REALM_ID and QUICKBOOKS_ACCESS_TOKEN (or REFRESH_TOKEN).")
        return

    # Start from a clean slate so the demo is reproducible.
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)

    # ── First sync: full backfill ──────────────────────────────────────────
    print("\n=== QuickBooks sync #1 (backfill) ===")
    source = quickbooks_source(
        realm_id=realm_id,
        access_token=access_token,
        refresh_token=refresh_token,
        include_invoices=True,
        include_bills=True,
        include_memos=True,
        environment=os.environ.get("QUICKBOOKS_ENVIRONMENT", "sandbox"),
    )

    print(f"Syncing accounting transactions into Cognee dataset '{DATASET_NAME}' ...")
    result = await cognee.remember(source, dataset_name=DATASET_NAME)
    print("Sync #1 result:", result)

    answer = await cognee.search(
        query_text="Summarize our outstanding invoices, client balances, and recent bills.",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nFinancial knowledge graph summary:\n", answer)

    # ── Second sync: incremental delta + forget-on-delete ──────────────────
    print("\n=== QuickBooks sync #2 (incremental + forget-on-delete) ===")
    source = quickbooks_source(
        realm_id=realm_id,
        access_token=access_token,
        refresh_token=refresh_token,
        include_invoices=True,
        include_bills=True,
        include_memos=True,
        environment=os.environ.get("QUICKBOOKS_ENVIRONMENT", "sandbox"),
    )
    result = await cognee.remember(source, dataset_name=DATASET_NAME)
    print("Sync #2 result:", result)

    answer = await cognee.search(
        query_text="What changed in our invoices or accounting transactions recently?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nUpdated financial summary:\n", answer)


if __name__ == "__main__":
    asyncio.run(main())
