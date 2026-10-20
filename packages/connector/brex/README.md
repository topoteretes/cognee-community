# Cognee Community Connector: Brex

A production-ready data-source connector for **Brex** corporate card expenses, employee memos, categories, receipt attachments, and budgets, built for Cognee's AI knowledge graph and vector indexing engine.

## Overview

Brex manages corporate cards, spend management, and departmental budgets. While numbers alone only represent accounting ledger entries, **employee memos, merchant contexts, and budget objectives contain organizational knowledge** (such as why tools were purchased, project codes, client dinners, and departmental spending ceilings).

This connector:
- Ingests Brex expenses, memos, and budget definitions as structured Markdown documents.
- Uses **Document Mode (`cognify`)** (`DOCUMENT_SOURCE_ATTR = "brex"`), ensuring financial entities and employee memos flow through entity extraction and knowledge graph construction.
- Supports **Incremental Sync** via `posted_at_start` watermarking stored in persistent `dlt` state.
- Supports **Forget-on-Delete** via full snapshot reconciliation (`write_disposition="replace"`), so refunded, reversed, or deleted expenses and closed budgets are pruned by Cognee's `orphan_cleanup`.

---

## Authentication

Generate an **API Token** in Brex:
1. Log in to your Brex dashboard.
2. Go to **Settings** > **Developer** > **Create Token**.
3. Select appropriate scopes: `expenses.card:read`, `budgets:read`.
4. Set the environment variable:

```bash
export BREX_API_KEY="your-brex-api-key"
```

Or pass `api_key` directly to `brex_source(api_key=...)`.

---

## Installation

```bash
pip install -e packages/connector/brex
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
from cognee_community_connector_brex import brex_source


async def main():
    source = brex_source(
        api_key=os.getenv("BREX_API_KEY"),
        include_expenses=True,
        include_budgets=True,
    )

    await cognee.add(source, dataset_name="brex_financials")
    await cognee.cognify(dataset_name="brex_financials")

    results = await cognee.search(
        search_type="INSIGHTS",
        query_text="What developer software was purchased and against which budget?",
        dataset_name="brex_financials",
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
| `api_key` | `str \| None` | `None` | Brex API token (falls back to `BREX_API_KEY` or `BREX_ACCESS_TOKEN`). |
| `posted_at_start` | `str \| None` | `None` | ISO8601 start date cutoff for expenses. |
| `posted_at_end` | `str \| None` | `None` | Optional ISO8601 end date cutoff. |
| `include_expenses` | `bool` | `True` | Whether to ingest corporate card expenses and memos. |
| `include_budgets` | `bool` | `True` | Whether to ingest corporate budgets and allocations. |
| `client` | `BrexClient \| None` | `None` | Preconfigured `BrexClient` instance. |

---

## Incremental Sync & Forget-on-Delete

1. **Incremental Sync**: The connector tracks `last_posted_at` in `dlt.current.resource_state()`, querying only expenses posted at or after the high-watermark on subsequent syncs.
2. **Forget-on-Delete**: Staging uses `write_disposition="replace"`. Deleted, voided, or refunded transactions drop out of the snapshot, allowing Cognee's `orphan_cleanup` to purge dead graph nodes and vector embeddings.

---

## Testing

Run unit tests:

```bash
pytest packages/connector/brex/tests/test_brex.py -v
```
