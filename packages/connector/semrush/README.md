# cognee-community-connector-semrush

Semrush data-source connector for [cognee](https://github.com/topoteretes/cognee) — sync domain rankings and organic keyword search intelligence into memory via full replace snapshots and historical period cursors.

## Overview

The Semrush connector syncs two core analytics reports into Cognee's relational manifest path:
1. **`domain_ranks`**: High-level domain overview (organic and paid keywords, traffic, and costs).
2. **`domain_organic`**: Detailed organic search keyword positions, volumes, CPC, and target URLs.

In Cognee, Semrush queries are scoped by a list of target `domains` plus a regional `database` code (e.g. `"us"`, `"uk"`).

## Architecture & Semantics

* **Relational Manifest Ingestion & Deterministic Scope**: The connector sets pipeline scope to "semrush" and leaves DOCUMENT_SOURCE_ATTR unset so dlt routes tables into Cognee's relational manifest path. The cursor stores {period, selection_fingerprint}; when unchanged, resources deselect themselves to emit 0 load packages, making zero HTTP calls and leaving destination tables untouched. To force an immediate reload, pass force=True.
* **Historical Period Snapshot**: Snapshots are keyed to Semrush's historical monthly data periods (YYYYMM15). When omitted, `display_date` defaults to the 15th of the previous calendar month.
* **Row Identity & Scope**: Each row's content hash includes `snapshot_date` (ISO YYYY-MM-15 derived strictly from the period). Row identities and graph nodes are stable within a period; across new periods, the graph is refreshed to the latest snapshot and old ids are pruned.
* **API Unit Costs**:
  * Because every request specifies a historical period snapshot (`display_date`), calls are billed at Semrush's historical data rate of approximately 50 API units per line (approximate; check Semrush's API units page).
  * Worked cost example:
    * Total estimated units = `domains * (1 + display_limit) * units_per_line`
    * For 2 domains with `display_limit = 5`: 2 * (1 + 5) * 50 = 600 API units.
    * For 2 domains with `display_limit = 10` (default): 2 * (1 + 10) * 50 = 1,100 API units.
  * References:
    * [Domain Overview (all databases)](https://developer.semrush.com/api/v3/seo/overview-reports/#domain-overview-all-databases)
    * [Domain Overview (one database)](https://developer.semrush.com/api/v3/seo/overview-reports/#domain-overview-one-database)
    * [Semrush API Units & Pricing Documentation](https://developer.semrush.com/api/v3/get-started/api-units-balance/) (approximate; check Semrush's API units page)
* **Strict Key Redaction**: API keys are scrubbed at the transport layer (`RedactingSession`) and stripped from URLs, exception messages, chained exception causes/contexts, debug logs, and all pipeline state/trace files.

## Parameters

`semrush_source` accepts the following arguments:
* **`domains`** (`list[str]`, required): List of target domain strings to query (e.g. `["github.com", "python.org"]`). Must be a list of non-empty strings (bare strings, non-list collections, and non-string items raise `ValueError`). Domains are automatically stripped, converted to lowercase, and deduplicated.
* **`api_key`** (`str | None`, default: `None`): Semrush API key. Falls back to `SEMRUSH_API_KEY` environment variable.
* **`database`** (`str`, default: `"us"`): Semrush regional database code (e.g. `"us"`, `"uk"`, `"de"`). Must be a non-empty string. Stripped and lowercased automatically. Both `domain_ranks` and `domain_organic` endpoints honor this parameter.
* **`display_limit`** (`int`, default: `10`): Maximum number of rows returned per domain. Must be a positive integer >= 1.
* **`display_date`** (`str | None`, default: `None`): Optional historical monthly period in YYYYMM15 format with a valid month (e.g. `"20260915"`). When omitted, defaults to the 15th of the previous calendar month.
* **`force`** (`bool`, default: `False`): When set to `True`, bypasses the skip gate even if period and selection fingerprint match stored state, forcing an immediate refetch and reload.
* **`session`** (`requests.Session | None`, default: `None`): Optional custom requests session. Defaults to `RedactingSession`, which inherits from dlt's `Session` and adds automatic key redaction, dlt runtime-configured retries, and CSV/ERROR response transformation. User-supplied sessions are used as-is and must handle their own key redaction.

## Known Behaviors & Limitations

* **All-ERROR-50 Protection**: If every requested domain returns `ERROR 50 :: NOTHING FOUND` on `domain_ranks` (e.g. brand-new domains or data not yet published for that period), the connector raises `SemrushNoDataError` with guidance to specify an earlier `display_date`. This prevents emitting an empty replace that would wipe destination tables and keeps graph memory intact without advancing the cursor.
* **Empty `domain_organic` or Header-Only CSV is Valid**: A domain with overview rank metrics but zero organic keywords (or returning a header-only CSV) is treated as an empty result `[]` and succeeds without error.
* **Per-Domain ERROR 50**: When querying multiple domains, if at least one domain returns rank data while others return `ERROR 50`, the run completes successfully. Domains returning `ERROR 50` yield 0 rows for those domains only, while successful domains are loaded normally.
* **Stale Organic Rows Limitation**: Stale organic rows can remain in the graph when a domain's organic results drop to zero, because Cognee's relational cleanup does not run on empty reads.
* **Empty Domain Pruning**: Removing all domains empties the destination dlt table, but Cognee's graph memory needs explicit pruning for that dataset.
* **Interrupted Pipeline Recovery**: If Cognee fails after dlt commits the cursor, the subsequent run skips due to the committed cursor: pass `force=True` to force an immediate reload.

## Installation

```bash
pip install "cognee-community-connector-semrush"
```

Requires `cognee>=1.6.3`, `dlt[sqlalchemy]>=1.16.0,<2`, and `requests>=2.28.0`.

## Usage

```python
import asyncio
import cognee
from cognee_community_connector_semrush import semrush_source


async def main():
    # Pass api_key directly or rely on SEMRUSH_API_KEY environment variable
    source = semrush_source(
        domains=["github.com", "python.org"],
        database="us",
        display_limit=5,
    )

    # Ingest into cognee memory
    await cognee.remember(
        source,
        dataset_name="seo_intelligence",
    )

    # Search cognee memory for ingested domain intelligence
    results = await cognee.search(
        query_text="What are the top organic keywords and ranking for github.com?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=["seo_intelligence"],
    )
    print(results)


if __name__ == "__main__":
    asyncio.run(main())
```

## Development

Run tests and linters locally:

```bash
uv run pytest tests/
uv run ruff check .
```
