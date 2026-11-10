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
- 🔐 **OAuth 2.0 Token Refresh & Safe Persistence**: Automatic bearer token refresh with `on_token_refreshed` callback to persist rotated refresh tokens into your secure vault or database.

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
from cognee_community_connector_quickbooks import QuickBooksClient, quickbooks_source

DATASET_NAME = "quickbooks_financial_memory"


def handle_token_refreshed(access_token: str, refresh_token: str) -> None:
    """Persist rotated OAuth refresh token to key vault or database."""
    print("QuickBooks rotated OAuth tokens. Persisting new refresh token...")
    # Example: vault.set("QB_REFRESH_TOKEN", refresh_token)


async def main():
    # 1. Configure client with auto-refresh callback
    client = QuickBooksClient(
        client_id=os.environ["QB_CLIENT_ID"],
        client_secret=os.environ["QB_CLIENT_SECRET"],
        refresh_token=os.environ["QB_REFRESH_TOKEN"],
        realm_id=os.environ["QB_REALM_ID"],
        sandbox=False,  # Set False for production (or set env QB_SANDBOX=false)
        on_token_refreshed=handle_token_refreshed,
    )

    # 2. Build dlt document source tagged for Cognee entity extraction
    source = quickbooks_source(
        client=client,
        entities=["Invoice", "Bill", "CreditMemo"],
        include_private_notes=False,  # Set True to ingest internal CFO/accountant notes
    )

    # 3. Ingest into Cognee memory
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",  # MANDATORY: prevents wiping data on incremental runs
        max_rows_per_table=0,  # MANDATORY: uncaps so orphan-cleanup sees the entire corpus
    )

    # 4. Query the financial knowledge graph
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
> - `sandbox=False` (or env `QB_SANDBOX=false`) connects to QuickBooks Online production API.

---

## OAuth Refresh Token Rotation & Persistence

Intuit rotates OAuth 2.0 refresh tokens upon usage. To avoid re-authentication prompts:
1. Pass `on_token_refreshed=callable(access_token, refresh_token)` to `QuickBooksClient`.
2. Persist the updated `refresh_token` in a persistent store (AWS Secrets Manager, HashiCorp Vault, PostgreSQL).
3. On subsequent executions, load the most recent refresh token from your store.

---

## Ingestion Architecture & Deletion Safety

1. **Document-Mode Routing**: The source is tagged with `cognee_document_source = "quickbooks"`. In `cognee.remember(...)`, this routes transactions through Document Mode into semantic chunks for LLM entity/relationship extraction.
2. **Two-Tier Deletion Reconciliation**:
   - Primary: Uses Intuit Change Data Capture (CDC) query for records modified or deleted within 30 days.
   - Secondary: If CDC is unavailable or returns an Intuit Fault (e.g. 30-day window expired), the connector safely falls back to a confirmed active-ID sweep.
   - Deletion Guardrail: If a client lacks full snapshot projection (`fetch_active_ids`), the connector fails closed during incremental syncs and will never infer deletions from a delta batch, completely preventing false tombstones.
3. **Anti-Corruption Threshold**: If a sweep returns 0 records when entities were previously known, tombstone emission is aborted immediately to protect existing knowledge graph memories.


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
