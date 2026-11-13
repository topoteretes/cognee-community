# Cognee Community Connector: Ramp

A production-ready data-source connector for **Ramp** corporate card transactions, expense memos, categories, and OCR receipt line items, built for Cognee's AI knowledge graph and vector indexing engine.

## Overview

Ramp is a finance automation and corporate card platform. While raw dollar amounts represent numbers, **expense memos, merchant categories, and OCR receipt line items contain critical organizational memory** (such as business purposes, project names, software subscriptions, and client entertainment).

This connector:
- Ingests Ramp transactions, employee memos, and OCR receipt line items as structured Markdown documents.
- Uses **Document Mode (`cognify`)** (`DOCUMENT_SOURCE_ATTR = "ramp"`), ensuring corporate spend flows through entity extraction and knowledge graph construction.
- Supports **Incremental Sync** via persistent watermarking stored in `dlt` state with an adjustable lookback window (default 30 days) to capture memos and receipts added days after the transaction.
- Supports **Forget-on-Delete** via full snapshot reconciliation (`write_disposition="replace"`), so refunded, declined, or deleted expenses are purged by Cognee's `orphan_cleanup`.

---

## Authentication

Ramp supports two authentication methods:

### Option 1: OAuth 2.0 Client Credentials (Recommended)
1. In your Ramp dashboard, navigate to **Settings** > **Developer** > **Create API Client**.
2. Select scopes: `transactions:read`, `receipts:read`.
3. Set environment variables:

```bash
export RAMP_CLIENT_ID="your-client-id"
export RAMP_CLIENT_SECRET="your-client-secret"
```

### Option 2: Direct API Access Token
```bash
export RAMP_ACCESS_TOKEN="your-ramp-access-token"
```

---

## Installation

```bash
pip install -e packages/connector/ramp
```

Or install with dependencies:

```bash
pip install "cognee==1.3.0" "dlt[sqlalchemy]>=1.9.0,<2" "httpx>=0.25.0,<1.0.0"
```

---

## Quickstart

```python
import asyncio
import os
import cognee
from cognee_community_connector_ramp import ramp_source


async def main():
    source = ramp_source(
        client_id=os.getenv("RAMP_CLIENT_ID"),
        client_secret=os.getenv("RAMP_CLIENT_SECRET"),
        include_receipts=True,
    )

    await cognee.add(source, dataset_name="ramp_expenses")
    await cognee.cognify(dataset_name="ramp_expenses")

    results = await cognee.search(
        search_type="INSIGHTS",
        query_text="What AI tool and cloud subscriptions did engineers charge last month?",
        dataset_name="ramp_expenses",
    )
    for res in results:
        print(res)


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Configuration Options

| Parameter | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `client_id` | `str \| None` | `None` | Ramp OAuth client ID (falls back to `RAMP_CLIENT_ID`). |
| `client_secret` | `str \| None` | `None` | Ramp OAuth client secret (falls back to `RAMP_CLIENT_SECRET`). |
| `access_token` | `str \| None` | `None` | Direct Bearer token (falls back to `RAMP_ACCESS_TOKEN`). |
| `from_date` | `str \| None` | `None` | ISO8601 start date cutoff. |
| `to_date` | `str \| None` | `None` | Optional ISO8601 end date cutoff. |
| `entity_id` | `str \| None` | `None` | Scope transactions to a specific Ramp legal entity. |
| `department_id`| `str \| None` | `None` | Filter by department ID. |
| `user_id` | `str \| None` | `None` | Filter by cardholder user ID. |
| `include_receipts`| `bool` | `True` | Fetch receipts and OCR line items for each transaction. |
| `lookback_days`| `int` | `30` | Days to look back on incremental syncs to capture delayed memos. |
| `skip_empty_memos`| `bool` | `False` | Omit transactions with neither memos nor receipts. |
| `client` | `RampClient \| None`| `None` | Preconfigured `RampClient` instance. |

---

## Incremental Sync & Forget-on-Delete

1. **Incremental Sync**: Transactions are ordered chronologically. The connector maintains `last_synced_time` in `dlt.current.resource_state()`. Because employees often add memos and receipts days after purchasing, each sync looks back `lookback_days` (default 30) from the cursor, updating changed documents.
2. **Forget-on-Delete**: Staging uses `write_disposition="replace"`. Voided, declined, or deleted transactions drop out of the active snapshot, prompting Cognee's `orphan_cleanup` to purge stale nodes and embeddings.

---

## Testing

Run unit tests:

```bash
pytest packages/connector/ramp/tests/test_ramp.py -v
```
