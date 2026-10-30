# Clay Data Connector for `cognee`

Data-source connector for ingesting structured table records from [Clay](https://www.clay.com) into `cognee` memory.

Built on [`dlt`](https://dlthub.com)'s declarative REST API source, this connector interfaces with Clay's Enterprise Table Query API (`POST /public/v0/tables/query`) and normalizes structured rows into `cognee` memory.

---

## Important Notices

### 1. Enterprise Plan Requirement
Clay's public Table Query API is **strictly available on Enterprise plans**.
- To query table records programmatically, your workspace must be on an Enterprise tier.
- The target table must have API access enabled under **Table Settings &rarr; Integrations**.
- Workspaces on Free, Starter, Pro, or Growth tiers will receive an HTTP 403 error.

### 2. Data Rights & Third-Party Enrichment Restrictions
Clay tables frequently aggregate enriched data provided by third-party data providers. Under Clay's Terms of Service:
- Users agree not to resell or commercially redistribute third-party provider data obtained from Clay.
- Provider data is generally restricted to internal CRM or workbench usage and may not be exported or retained outside authorized contexts without an explicit provider license.
- **Recommended Practice**: Callers should supply the `fields` filter parameter to sync only their own first-party customer data (e.g. uploaded internal IDs, company names, account owners), avoiding unauthorized downstream retention of third-party enrichments.

---

## Installation

Install the connector within your Python environment:

```bash
pip install cognee-community-connector-clay
```

---

## Configuration & Credentials

Set the following environment variables or supply them directly to the `clay_source` factory:

| Variable | Description |
| :--- | :--- |
| `CLAY_API_KEY` | Your Clay API key with table query permissions. |
| `CLAY_TABLE_ID` | The ID of the table to query (e.g., `t_0te9i4tZEHwc9hihBXu`), visible in the Clay table URL. |

---

## Quickstart

```python
import asyncio
import os
import cognee
from cognee_community_connector_clay import clay_source


async def main():
    # Configure the Clay source selecting specific first-party columns
    source = clay_source(
        table_id=os.getenv("CLAY_TABLE_ID", "t_0te9i4tZEHwc9hihBXu"),
        api_key=os.getenv("CLAY_API_KEY"),
        fields=["Company Name", "Domain", "Account Owner"],
        primary_key="domain",
        write_disposition="replace",
    )

    # Ingest into cognee memory
    await cognee.remember(
        source,
        dataset_name="clay_accounts",
        max_rows_per_table=0,  # 0 disables cognee's 50-row default cap for full-table sync
    )


if __name__ == "__main__":
    asyncio.run(main())
```

> **Note**: Setting `max_rows_per_table=0` in `cognee.remember()` is recommended for full-table syncs. Without it, Cognee applies a default limit of 50 records per table, which would truncate larger tables.

---

## API Reference

### `clay_source(...)`

```python
def clay_source(
    table_id: str | None = None,
    *,
    api_key: str | Callable[[], str] | None = None,
    fields: list[str] | None = None,
    primary_key: str | None = None,
    write_disposition: str = "replace",
    limit: int = 100,
    base_url: str = "https://api.clay.com/public/v0",
    check_active: Callable[[], None] | None = None,
) -> dlt.sources.DltResource: ...
```

| Parameter | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `table_id` | `str \| None` | `None` | Target table ID. Falls back to `CLAY_TABLE_ID`. |
| `api_key` | `str \| Callable[[], str] \| None` | `None` | API key or token provider callable. Falls back to `CLAY_API_KEY`. Never logged. |
| `fields` | `list[str] \| None` | `None` | Columns to include. Formatted into the `query.select` payload. If omitted, all columns are queried and a warning is logged. |
| `primary_key` | `str \| None` | `None` | Column to use as business identifier. Takes precedence over system IDs; falls back to system record ID if absent/null, or deterministic hash. |
| `write_disposition` | `str` | `"replace"` | `dlt` sync disposition. `"replace"` performs full table snapshot sync. |
| `limit` | `int` | `100` | Rows requested per page (1–100). |
| `base_url` | `str` | `"https://api.clay.com/public/v0"` | Base URL for Clay public API. |
| `check_active` | `Callable[[], None] \| None` | `None` | Optional host authorization checkpoint called per row to allow clean cancellation without corrupted state. |

---

## Running Offline Tests

The test suite runs 100% offline without live network dependencies or API keys:

```bash
python -m pytest packages/connector/clay/tests -v
```
