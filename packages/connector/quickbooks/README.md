# cognee-community-connector-quickbooks

A QuickBooks Online data-source connector for [cognee](https://github.com/topoteretes/cognee):
turn your financial ledgers and accounting records into AI memory — "ask my QuickBooks".

It exposes a `dlt` source you hand straight to `cognee.remember(...)`, integrating directly into
cognee's DLT ingestion subsystem (`resolve_dlt_sources` → `ingest_dlt_source` → `orphan_cleanup`).
Transactions (invoices, vendor bills, credit memos, and line items) are formatted into rich semantic
Markdown cards using **Document Mode** (`DOCUMENT_SOURCE_ATTR = "quickbooks"`), allowing cognee's
cognify pipeline to extract entities, relationships, and knowledge graph memories.

---

## Features

- 📑 **Dual-Layer Entity Extraction**: Ingests Invoices, Bills, Credit Memos, and customer/vendor notes as semantic markdown cards into Cognee's Knowledge Graph.
- ⏱️ **Incremental Sync**: Uses QuickBooks `Metadata.LastUpdatedTime` cursor to fetch only new or modified transactions on subsequent runs.
- 🗑️ **Forget-on-Delete**: Deletions upstream in QuickBooks emit `_deleted=True` tombstones (via Change Data Capture API and snapshot sweeps), automatically prompting `orphan_cleanup` to purge stale nodes and embeddings.
- 🛡️ **Anti-Corruption Guardrails**: If an Intuit query or sweep encounters transient network failures or returns empty when records were previously known, tombstone emission is aborted immediately to protect existing memory.
- 🔐 **OAuth 2.0 Token Refresh**: Automatic bearer token refresh using client credentials and refresh token.

---

## Installation

```bash
uv pip install cognee-community-connector-quickbooks
# Or install locally from this monorepo:
cd packages/connector/quickbooks && pip install -e .
```

---

## Usage

```python
import asyncio
import os
import cognee
from cognee_community_connector_quickbooks import quickbooks_source

DATASET_NAME = "quickbooks_financial_memory"

async def main():
    # Configure source with Intuit OAuth credentials
    source = quickbooks_source(
        client_id=os.environ["QB_CLIENT_ID"],
        client_secret=os.environ["QB_CLIENT_SECRET"],
        refresh_token=os.environ["QB_REFRESH_TOKEN"],
        realm_id=os.environ["QB_REALM_ID"],
        sandbox=True,  # Set False for production
        entities=["Invoice", "Bill", "CreditMemo"],
    )

    # Ingest into memory
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",  # MANDATORY: prevents wiping data on incremental runs
        max_rows_per_table=0,       # MANDATORY: uncaps so orphan-cleanup sees the entire corpus
    )

    # Query the financial knowledge graph
    answer = await cognee.search(
        query_text="Which customer invoices have outstanding balances and what are the payment terms?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("Answer:\n", answer)

if __name__ == "__main__":
    asyncio.run(main())
```

> **Important Ingestion Flags:**
> - `write_disposition="merge"` is required for incremental sync and hard-delete tombstones.
> - `max_rows_per_table=0` disables the row ceiling during DLT reads so `orphan_cleanup` reconciles against the complete corpus.

---

## One-Time Setup (Intuit Developer App & OAuth)

1. Create a developer account at [developer.intuit.com](https://developer.intuit.com/).
2. Under **Dashboard**, click **Create an app** and select **QuickBooks Online and Payments**.
3. In your app settings, navigate to **Development -> Keys & credentials** to find your **Client ID**, **Client Secret**, and Sandbox **Company ID (realmId)**.
4. Open the [Intuit OAuth 2.0 Playground](https://developer.intuit.com/app/developer/playground):
   - Select your app.
   - Select the `com.intuit.quickbooks.accounting` scope.
   - Click **Get authorization code** and connect your Sandbox company.
   - Click **Get tokens** to generate your `refresh_token`.
5. Export connection environment variables:
   ```bash
   export QB_CLIENT_ID="AB..."
   export QB_CLIENT_SECRET="xy..."
   export QB_REFRESH_TOKEN="rt_..."
   export QB_REALM_ID="1234567890"
   export LLM_API_KEY="sk-..."
   ```

---

## Testing

Run the hermetic unit test suite (backed by mock Intuit responses and offline DuckDB pipeline):

```bash
pytest tests/
```
