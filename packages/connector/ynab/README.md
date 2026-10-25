# YNAB (You Need A Budget) Connector for Cognee

A `dlt` data-source connector that ingests budgets, transactions, category groups, and spending activity from YNAB into Cognee memory.

Part of the **Cognee Mergetober Hackathon** (`#6062`).

---

## Features

- **Transaction Ingestion:** Ingests transaction history including amounts, payees, categories, accounts, cleared status, memos, and split subtransactions.
- **Budget Categories & Goals:** Ingests budget category groups, budgeted monthly amounts, activity/spending, available balances, and target goals.
- **Currency Normalization:** Converts YNAB internal integer milliunits to standard currency values.
- **Full Snapshot & Replace:** Uses `write_disposition="replace"` for clean updates and synchronization.
- **Document Mode:** Tags resources with `DOCUMENT_SOURCE_ATTR` to route directly through Cognee's `cognify` entity-extraction pipeline for semantic financial memory.

---

## Quickstart

```python
import asyncio
import os
import cognee
from cognee_community_connector_ynab import ynab_source

async def main():
    # 1. Initialize YNAB source with your Personal Access Token
    source = ynab_source(
        access_token=os.getenv("YNAB_ACCESS_TOKEN"),
        since_date="2026-01-01",
    )

    # 2. Add to Cognee memory
    await cognee.add(source)

    # 3. Cognify into knowledge graph
    await cognee.cognify()

    # 4. Search financial memory
    results = await cognee.search("Which categories have the highest spending this month?")
    for res in results:
        print("-", res)

if __name__ == "__main__":
    asyncio.run(main())
```

---

## Running Tests

Run the hermetic offline test suite:

```bash
pytest packages/connector/ynab/tests
```
