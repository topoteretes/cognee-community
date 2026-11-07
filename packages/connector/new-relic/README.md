# cognee-community-connector-new-relic

A New Relic observability data-source connector for [cognee](https://github.com/topoteretes/cognee):
synchronize alert policies, NRQL conditions, dashboards, and incident events into memory.

Exposes a DLT resource directly compatible with `cognee.remember(...)` and `cognee.add(...)`.
Observability configurations and telemetry context are rendered into structured Markdown and
ingested as **documents** (routed through Cognee's cognify entity extraction and knowledge graph).

## Features

- **NerdGraph GraphQL Ingestion**: Queries New Relic's modern NerdGraph GraphQL API (`/graphql`) rather than legacy REST APIs.
- **Full Alert Policies & Conditions**: Ingests policies, incident preferences, and associated NRQL threshold conditions with runbook URLs.
- **Dashboards**: Indexes dashboard metadata, descriptions, page layouts, and widget definitions.
- **Incident & Issue History**: Ingests operational incidents, priority levels, sources, and timestamps.
- **Incremental Incident Sync**: Tracks incident timestamp cursors in DLT state to ingest newly activated issues without reprocessing historical events.
- **Authoritative Forget-on-Delete**: Authoritative inventories (dashboards and policies) emit `{"id": ..., "_deleted": True}` tombstones when deleted upstream, removing orphaned graph nodes and vector points.
- **Multi-Region Support**: Seamlessly supports New Relic US (`https://api.newrelic.com/graphql`) and EU (`https://api.eu.newrelic.com/graphql`) regions.
- **Automatic Retries**: Exponential backoff and jitter on rate limits (HTTP 429) and transient server errors.

## Installation

```bash
uv pip install cognee-community-connector-new-relic
# or within the monorepo:
cd packages/connector/new-relic && uv sync
```

## Setup

1. Obtain a New Relic **User API Key**:
   - Log into New Relic One.
   - Go to your profile icon -> **API keys**.
   - Create a **User** key (prefix `NRAK-`). Note: Ingest keys are not supported for NerdGraph entity queries.
2. Find your **Account ID** in the top navigation or account switcher.
3. Export environment variables:
   ```bash
   export NEW_RELIC_API_KEY="NRAK-..."
   export NEW_RELIC_ACCOUNT_ID="1234567"
   export LLM_API_KEY="sk-..."
   ```

## Usage

```python
import cognee
from cognee_community_connector_new_relic import new_relic_source

# Connect to New Relic and sync all resources (or specify resources=['policies', 'dashboards'])
source = new_relic_source(region="US")

await cognee.remember(
    source,
    dataset_name="new_relic_observability",
)

# Ask questions across your observability stack
results = await cognee.search(
    query_text="Which alert conditions monitor latency and what are their thresholds?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["new_relic_observability"],
)
print(results)
```

## Testing

Run the offline test suite (no live New Relic key needed):

```bash
cd packages/connector/new-relic
uv run pytest tests/
```

