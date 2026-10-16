# Mixpanel Data-Source Connector for Cognee

The `cognee-community-connector-mixpanel` package allows you to sync Mixpanel Lexicon event schemas, user cohorts, and saved report/bookmark definitions directly into Cognee's AI memory graph and vector store.

---

## Installation

```bash
uv pip install cognee-community-connector-mixpanel
```

---

## Prerequisites & Authentication

Mixpanel supports authentication via Service Accounts (recommended) or legacy API Secrets:

1. Create a Service Account in your Mixpanel project under **Project Settings > Service Accounts**.
2. Set the following environment variables:

```bash
export MIXPANEL_PROJECT_ID="your_project_id"
export MIXPANEL_SERVICE_ACCOUNT_USERNAME="your_service_account_username"
export MIXPANEL_SERVICE_ACCOUNT_SECRET="your_service_account_secret"
```

For EU-residency projects, specify the EU endpoint:
```python
source = mixpanel_source(base_url="https://eu.mixpanel.com/api")
```

---

## Usage Example

```python
import asyncio
import os
import cognee
from cognee_community_connector_mixpanel import mixpanel_source


async def main():
    source = mixpanel_source(
        include_schemas=True,
        include_cohorts=True,
        include_reports=True,
    )

    await cognee.remember(
        source,
        dataset_name="mixpanel_analytics_memory",
    )

    # Query your analytics taxonomy and definitions
    results = await cognee.recall(
        "What properties and events define our core conversion funnel?",
        dataset_name="mixpanel_analytics_memory",
    )
    print(results)


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Key Features

- **Knowledge-Centric Ingestion:** Specifically targets analytical definitions and schemas (Lexicon event schemas, property descriptions, cohort target criteria, and saved query formulas) rather than dumping billions of raw event logs.
- **Configurable Resource Streams:** Selectively enable `include_schemas`, `include_cohorts`, or `include_reports`.
- **Incremental Synchronization:** Supports `since` timestamp parameters to only ingest newly created or modified schemas and cohorts.
- **Safe Lifecycle:** `write_disposition="replace"` ensures deleted cohorts or archived reports upstream automatically trigger Cognee graph cleanup.
- **Offline Mocked Test Suite:** Verified against mocked Mixpanel REST schemas for offline CI execution.

---

## Running Tests

```bash
pytest packages/connector/mixpanel/tests/
```
