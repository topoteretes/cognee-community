# cognee-community-connector-quickbooks

QuickBooks Online data-source connector for **[Cognee](https://github.com/topoteretes/cognee)** — ingest invoices, bills, and memos into memory with incremental sync and forget-on-delete.

## Overview

QuickBooks Online manages company financial accounting, customer billing, and vendor payables. This connector maps QuickBooks Online transactions into structured knowledge graph documents within Cognee, enabling AI agents to answer financial and billing questions like:
- *"What invoices are currently outstanding and what is the total remaining customer balance?"*
- *"Which vendors did we receive bills from this month and for which services?"*
- *"Summarize any credit memos or customer adjustments applied recently."*

## Features

- **Document Mode**: Tags the DLT source with `DOCUMENT_SOURCE_ATTR = "quickbooks"`, allowing transactions to flow through the full `cognify` entity-extraction pipeline and knowledge graph rather than raw tabular records.
- **Entity Coverage**: Ingests **Invoices**, **Bills**, and **Credit Memos**, preserving line items (quantities, rates, item descriptions), dates, totals, remaining balances, and both customer memos and private notes.
- **Intuit OAuth 2.0 & Auto-Refresh**: Supports direct Bearer access tokens as well as automatic refresh via Intuit's OAuth 2.0 token endpoint using `refresh_token`, `client_id`, and `client_secret`.
- **Company Realm Isolation**: Scopes all documents to the company `realm_id` (`quickbooks:{realm_id}:{entity}:{id}`) and supports both `sandbox` and `production` environments.
- **Incremental Sync**: Queries use `MetaData.LastUpdatedTime > '...'` and automatically persist/advance the watermark cursor via `dlt.current.resource_state()` so subsequent syncs fetch only updated or newly created records.
- **Full Snapshot & Forget-on-Delete**: Resources declare `write_disposition="replace"`. Voided or deleted transactions upstream fall out of the snapshot, allowing Cognee's `orphan_cleanup` to prune dead nodes and vectors from memory.
- **Resilient Auto-Pagination & Retries**: Queries auto-paginate across Intuit's 1-indexed pagination (`STARTPOSITION` / `MAXRESULTS`) and retry on HTTP 429 rate limits (honoring `Retry-After`) and transient server errors.

## Intuit Developer App & Sandbox Setup

1. Create a free developer account at [developer.intuit.com](https://developer.intuit.com).
2. Go to **Dashboard** → **Create an app** and select the **Accounting** scope.
3. Access your **Sandbox** company under **Dashboard** → **Sandbox**.
4. Generate credentials using the [Intuit OAuth 2.0 Playground](https://developer.intuit.com/app/developer/playground):
   - Select your App and the `com.intuit.quickbooks.accounting` scope.
   - Authorize connection to your Sandbox company.
   - Note the **realmId** (Company ID), **Access Token**, and **Refresh Token**.
5. Export your environment variables:

```bash
export QUICKBOOKS_REALM_ID="1234567890"
export QUICKBOOKS_ACCESS_TOKEN="ey..."
# Or for automatic OAuth token rotation:
export QUICKBOOKS_CLIENT_ID="AB..."
export QUICKBOOKS_CLIENT_SECRET="cd..."
export QUICKBOOKS_REFRESH_TOKEN="rt..."
```

## Installation

```bash
uv pip install cognee-community-connector-quickbooks
# OR
pip install cognee-community-connector-quickbooks
```

## Quickstart

```python
import asyncio
import os
import cognee
from cognee_community_connector_quickbooks import quickbooks_source


async def main():
    source = quickbooks_source(
        realm_id=os.environ["QUICKBOOKS_REALM_ID"],
        access_token=os.environ.get("QUICKBOOKS_ACCESS_TOKEN"),
        refresh_token=os.environ.get("QUICKBOOKS_REFRESH_TOKEN"),
        include_invoices=True,
        include_bills=True,
        include_memos=True,
        environment="sandbox",
    )

    await cognee.remember(source, dataset_name="quickbooks_accounting")

    # Query the memory graph
    results = await cognee.search(
        "Summarize our outstanding client invoices and balances.",
        dataset_name="quickbooks_accounting",
    )
    print(results)


if __name__ == "__main__":
    asyncio.run(main())
```

## Configuration & Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `realm_id` | `str \| None` | `None` | QuickBooks company realmId. Falls back to `QUICKBOOKS_REALM_ID`. |
| `access_token` | `str \| None` | `None` | Intuit OAuth access token. Falls back to `QUICKBOOKS_ACCESS_TOKEN`. |
| `refresh_token` | `str \| None` | `None` | Intuit OAuth refresh token. Falls back to `QUICKBOOKS_REFRESH_TOKEN`. |
| `client_id` | `str \| None` | `None` | Intuit App Client ID. Falls back to `QUICKBOOKS_CLIENT_ID`. |
| `client_secret` | `str \| None` | `None` | Intuit App Client Secret. Falls back to `QUICKBOOKS_CLIENT_SECRET`. |
| `environment` | `str` | `"sandbox"` | Environment: `"sandbox"` or `"production"`. |
| `base_url` | `str \| None` | `None` | Optional base URL override (for tests or proxies). |
| `include_invoices` | `bool` | `True` | Sync customer invoices. |
| `include_bills` | `bool` | `True` | Sync vendor bills. |
| `include_memos` | `bool` | `True` | Sync credit memos and transaction notes. |
| `invoice_ids` | `list[str] \| None` | `None` | Restrict ingestion to specific invoice IDs. |
| `bill_ids` | `list[str] \| None` | `None` | Restrict ingestion to specific bill IDs. |
| `memo_ids` | `list[str] \| None` | `None` | Restrict ingestion to specific credit memo IDs. |
| `since` | `str \| None` | `None` | ISO-8601 timestamp string for incremental sync (`MetaData.LastUpdatedTime > since`). Managed automatically via dlt state if omitted. |
| `client` | `Any` | `None` | Optional pre-configured client for dependency injection in tests. |

## Running Tests

Run the test suite using `uv`:

```bash
uv --directory packages/connector/quickbooks run --with pytest pytest -v tests
```
