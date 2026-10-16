# Pendo Data-Source Connector for Cognee

The `cognee-community-connector-pendo` package allows you to sync product walkthrough guides, feature feedback requests, and qualitative NPS survey comments directly from Pendo into Cognee's AI memory graph and vector store.

---

## Installation

```bash
uv pip install cognee-community-connector-pendo
```

---

## Prerequisites & Authentication

1. Generate an Integration Key in your Pendo subscription under **Settings > Integrations > Integration Keys**.
2. Set your environment variable:

```bash
export PENDO_INTEGRATION_KEY="your_pendo_integration_key"
```

If your Pendo subscription is hosted in the EU region, you can configure the base URL accordingly:
```python
source = pendo_source(base_url="https://app.eu.pendo.io/api/v1")
```

---

## Usage Example

```python
import asyncio
import os
import cognee
from cognee_community_connector_pendo import pendo_source


async def main():
    # Sync guides, feedback items, and qualitative NPS responses
    source = pendo_source(
        include_guides=True,
        include_feedback=True,
        include_nps=True,
    )

    await cognee.remember(
        source,
        dataset_name="pendo_feedback_memory",
    )

    # Ask questions across your product feedback and NPS comments
    results = await cognee.recall(
        "What are the top UI frustrations mentioned by detractors in recent NPS comments?",
        dataset_name="pendo_feedback_memory",
    )
    print(results)


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Key Features

- **High-Signal Qualitative Ingestion:** Explicitly focuses on knowledge-bearing qualitative text (guide steps, feedback descriptions, and freeform NPS comments), filtering out raw telemetry noise.
- **Configurable Resource Streams:** Selectively enable or disable `include_guides`, `include_feedback`, or `include_nps`.
- **Incremental Synchronization:** Supports `since` timestamp parameters to only fetch newly updated guides, feature requests, and recent survey responses.
- **Automated Lifecycle Management:** `write_disposition="replace"` ensures that deleted guides or pruned feedback items upstream cleanly trigger Cognee graph cleanup.
- **Offline Mocked Test Suite:** Fully verified against mocked REST response schemas for CI pipelines.

---

## Running Tests

```bash
pytest packages/connector/pendo/tests/
```
