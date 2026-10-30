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
- 🗑️ **Resilient Forget-on-Delete**: Deletions upstream in QuickBooks emit `_deleted=True` tombstones via Change Data Capture (CDC) API, backed by active ID sweeps if CDC is unavailable or returns an incomplete delta.
- 🛡️ **Anti-Corruption Guardrails**: If an Intuit query or sweep encounters transient network failures or returns empty when records were previously known, tombstone emission is aborted immediately to protect existing memory.
- 🏢 **Multi-Company & Realm Isolation**: Scoped via `PIPELINE_SCOPE_ATTR` (`resource_name` / `realm_id`) and realm-namespaced IDs (`quickbooks:<realm_id>:...`) so multiple companies never collide in the same dataset.
- 🔒 **Privacy Controls**: Internal `PrivateNotes` are excluded from ingestion by default, with an opt-in `include_private_notes=True` parameter. Sensitive credentials or bank details are never ingested.
- 🔐 **OAuth 2.0 Token Refresh**: Automatic bearer token refresh using client credentials and refresh token.

---

## Installation

```bash
uv pip install cognee-community-connector-quickbooks
# Or install locally from this monorepo:
cd packages/connector/quickbooks && uv sync --all-extras
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
        include_private_notes=False,  # Set True to ingest internal CFO/accountant notes
    )

    # Ingest into memory
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",  # MANDATORY: prevents wiping data on incremental runs
        max_rows_per_table=0,  # MANDATORY: uncaps so orphan-cleanup sees the entire corpus
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

## Privacy & Field Allowlist

| Field / Object | Ingested | Details |
| :--- | :---: | :--- |
| **Invoice / Bill Numbers** | ✅ Yes | `DocNumber`, transaction dates, due dates |
| **Customer / Vendor Names** | ✅ Yes | Extracted from `CustomerRef` and `VendorRef` |
| **Line Items & Descriptions** | ✅ Yes | Quantity, unit price, item name, and line descriptions |
| **Memos & Payment Terms** | ✅ Yes | External `CustomerMemo` and status |
| **Private Notes** | ⚙️ Opt-in | Excluded by default; opt-in with `include_private_notes=True` |
| **Credit Card / Bank Account Info** | ❌ Never | Never ingested |

---

## Running Offline Tests

The test suite runs 100% offline without live network connections or QuickBooks credentials:

```bash
uv run pytest tests/ -v
```
