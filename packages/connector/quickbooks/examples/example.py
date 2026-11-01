"""QuickBooks Online connector demo — "Ask my financial ledger".

Syncs QuickBooks Online invoices, vendor bills, and credit memos into Cognee
memory incrementally with forget-on-delete.

────────────────────────────────────────────────────────────────────────────
One-Time Setup
────────────────────────────────────────────────────────────────────────────
1. Create an Intuit Developer account at https://developer.intuit.com/
2. Create an App under "Dashboard" and select QuickBooks Online and Payments API.
3. Obtain your Client ID, Client Secret, and Sandbox Company ID (realmId) under
   Development -> Keys & credentials.
4. Use the Intuit OAuth 2.0 Playground to generate a Refresh Token:
   https://developer.intuit.com/app/developer/playground
5. Export connection credentials:

       export QB_CLIENT_ID="AB..."
       export QB_CLIENT_SECRET="xy..."
       export QB_REFRESH_TOKEN="rt_..."
       export QB_REALM_ID="1234567890"
       export LLM_API_KEY="sk-..."

Run it:

    python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_quickbooks import quickbooks_source

DATASET_NAME = "quickbooks_financial_memory"

QUICKBOOKS_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,  # 0 = uncap so orphan-cleanup reconciles against full corpus
}


async def main() -> None:
    client_id = os.environ.get("QB_CLIENT_ID")
    client_secret = os.environ.get("QB_CLIENT_SECRET")
    refresh_token = os.environ.get("QB_REFRESH_TOKEN")
    realm_id = os.environ.get("QB_REALM_ID")

    if not all([client_id, client_secret, refresh_token, realm_id]):
        print(
            "QuickBooks credentials missing.\n"
            "Please export QB_CLIENT_ID, QB_CLIENT_SECRET, QB_REFRESH_TOKEN, and QB_REALM_ID.\n"
            "See the setup instructions in this file's docstring."
        )
        return

    # Reset data for reproducible demo
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)

    def build_source():
        return quickbooks_source(
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=refresh_token,
            realm_id=realm_id,
            sandbox=True,
            entities=["Invoice", "Bill", "CreditMemo"],
        )

    # ── Sync #1: Full backfill ──────────────────────────────────────────────
    print("\n=== QuickBooks Sync #1: Full Backfill ===")
    result = await cognee.remember(
        build_source(),
        dataset_name=DATASET_NAME,
        **QUICKBOOKS_REMEMBER_KWARGS,
    )
    print("Ingestion result:", result)

    # ── Search ─────────────────────────────────────────────────────────────
    print("\n=== Querying Financial Memory ===")
    answer = await cognee.search(
        query_text="Summarize the largest open invoices and any customer memo notes.",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("Graph Completion Answer:\n", answer)

    # ── Sync #2: Incremental delta + Forget-on-Delete ───────────────────────
    # Re-running with the SAME dataset resumes from the cursor: only transactions
    # modified since Sync #1 are fetched, and deleted transactions are purged.
    print("\n=== QuickBooks Sync #2: Incremental Re-sync ===")
    result2 = await cognee.remember(
        build_source(),
        dataset_name=DATASET_NAME,
        **QUICKBOOKS_REMEMBER_KWARGS,
    )
    print("Incremental result:", result2)


if __name__ == "__main__":
    asyncio.run(main())
