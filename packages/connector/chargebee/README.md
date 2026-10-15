# Chargebee Data-Source Connector

This is a `dlt` data-source connector for [Chargebee](https://www.chargebee.com/). It ingests structured billing data (Customers, Subscriptions, Invoices) via the Chargebee REST API into Cognee.

## Features
- **Relational Path:** Leaves `DOCUMENT_SOURCE_ATTR` unset to preserve structured tabular data and bypass unnecessary LLM extraction.
- **Incremental Sync:** Uses the `updated_at` filter to only fetch records modified since the last sync.
- **Forget-on-Delete:** Natively maps Chargebee's `deleted: true` flag to `dlt` soft-delete tombstones (`_delete_dlt_orphans`), ensuring upstream deletions propagate into the graph seamlessly without full snapshots.

## Setup

1. Create a Chargebee test site.
2. Generate an API Key in your Chargebee dashboard under **Settings > Configure Chargebee > API Keys**.
3. Note your site name (the subdomain in your Chargebee URL, e.g., `your-site` if the URL is `your-site.chargebee.com`).

## Running the Example

Navigate to the `examples` directory and run the example script:

```bash
export CHARGEBEE_API_KEY="your_api_key_here"
export CHARGEBEE_SITE="your_site_name_here"

python examples/example.py
```