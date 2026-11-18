# cognee-community-connector-brex

A Brex data-source connector for [cognee](https://github.com/topoteretes/cognee): turn corporate expenses, receipt notes, user memos, and departmental budgets into searchable AI memory.

Syncs corporate spend data incrementally via Brex's official `updated_at_start` cursor, with **forget-on-delete** orphan cleanup.

---

## Features

- **Bearer Token Authentication**: Uses Brex User or Admin API tokens with `expenses.read` and `budgets.read` scopes.
- **Official Production Endpoints**: Targets Brex production API (`https://api.brex.com`) with `GET /v1/expenses` and `GET /v2/budgets`.
- **Dual-Path Ingestion Model**:
  - **Document Mode (`brex_memos_source`)**: Renders transaction notes, user justifications, merchants, employee details, and categories into Markdown document cards (`DOCUMENT_SOURCE_ATTR = "brex"`). These flow directly into Cognee's text chunking and LLM knowledge graph extraction.
  - **Structured Relational Mode (`brex_source`)**: Ingests tabular financial records (`brex_expenses`, `brex_budgets`) with normalized foreign keys (`budget_id`, `parent_budget_id`) and typed fields (`amount`, `currency`, `category`, `status`).
- **Incremental High-Watermark Sync**: Leverages Brex's `updated_at_start` filter and cursor pagination to fetch only changed, refunded, or newly modified expenses on subsequent runs, accumulating new IDs in resource state.
- **Multi-Account State Isolation**: Optional `account_id` parameter automatically namespaces global IDs (`brex:{account_id}:...`) and isolates DLT pipeline state, preventing cross-account state or watermark collision.
- **Forget-on-Delete Reconciliation**: On full sweeps (`full_refresh=True`), the connector reconciles active records against `known_ids`. Missing or voided IDs emit hard-delete tombstones (`{"_deleted": True}`) that drive Cognee's `orphan_cleanup` across graph, vector, and relational stores.
- **Anti-Corruption & Empty-Sweep Guardrails**:
  - If an API request fails, times out, or encounters rate limits, deletion reconciliation is **aborted immediately** without emitting tombstones.
  - If a sweep returns 0 records when previous records were known, deletion is skipped to protect against transient empty responses, unless `allow_empty_sweep=True`.

> [!CAUTION]
> Setting `allow_empty_sweep=True` bypasses empty-sweep safeguards. If an upstream Brex API call returns 0 items due to transient network or permission changes, all previously synced records will be tombstoned. Only enable this flag when explicitly intending to purge an entire corpus.

---

## Installation

```bash
uv pip install cognee-community-connector-brex
# or from this monorepo:
cd packages/connector/brex && uv sync --all-extras
```

---

## Setting Up Brex API Credentials

1. Log in to your **[Brex Dashboard](https://dashboard.brex.com/)**.
2. Navigate to **Settings** $\rightarrow$ **Developer** $\rightarrow$ **API Keys**.
3. Create a new **User API Key** or **Admin Token**.
4. Grant the following read-only scopes:
   - `expenses.read`
   - `budgets.read`
5. Copy the generated token and export it in your environment:
   ```bash
   export BREX_API_TOKEN="b_usr_your_token_here"
   ```

---

## Usage

```python
import asyncio
import os
import cognee
from cognee_community_connector_brex import brex_memos_source, brex_source


async def main():
    token = os.environ["BREX_API_TOKEN"]

    # 1. Structured relational financial data (expenses & budgets)
    spend_source = brex_source(
        token=token,
        include=["expenses", "budgets"],
        account_id="primary_corp",
    )

    # 2. Semantic document cards for LLM knowledge graph extraction
    memos_source = brex_memos_source(
        token=token,
        account_id="primary_corp",
    )

    # Ingest into Cognee memory
    await cognee.remember(
        [spend_source, memos_source],
        dataset_name="brex_spend",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,  # disable row limit so orphan cleanup sees entire corpus
    )

    # Query corporate spend memory
    answer = await cognee.search(
        query_text="What was the business purpose for our recent Datadog infrastructure charges?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=["brex_spend"],
    )
    print(answer)


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Supported Resources & Field Mappings

| Resource | Type | Global ID Format | Key Fields Synced |
| :--- | :--- | :--- | :--- |
| **`brex_memos`** | Document Card | `brex:{account_id}:memo:{id}` | Merchant, Cardholder, Department, Amount, Memo/Justification, Budget |
| **`brex_expenses`** | Relational | `brex:{account_id}:expense:{id}` | `amount`, `currency`, `merchant_name`, `category`, `user_name`, `budget_id`, `updated_at`, `status` |
| **`brex_budgets`** | Relational | `brex:{account_id}:budget:{id}` | `name`, `description`, `limit_amount`, `period_type` (recurrence), `parent_budget_id`, `status` |

*Note: Brex v2 budgets API exposes configured target amounts, not live spend.*

---

## Synchronization & Deletion Operational Guide

1. **Incremental Sync (Default)**:
   - Uses Brex's `updated_at_start` filter to fetch newly created, refunded, or edited expenses since the last watermark.
   - Accumulates newly observed IDs into `known_ids`.
   - Deletion tombstones are **not emitted during incremental runs** to prevent false deletions on partial delta queries.
2. **Periodic Deletion Reconciliation (`full_refresh=True`)**:
   - Performs a complete sweep of active records and computes `known_ids - active_ids`.
   - Emits tombstones (`{"_deleted": True}`) for missing or voided transactions, driving Cognee's `orphan_cleanup`.
3. **Recommended Production Schedule**:
   - **Hourly/Continuous**: Run standard incremental sync (`full_refresh=False`).
   - **Daily/Weekly Maintenance Job**: Run a scheduled reconciliation run with `full_refresh=True`.

---

## Running Offline Tests

The test suite runs 100% offline without live network connections or Brex credentials:

```bash
python -m pytest tests/ -v
```
