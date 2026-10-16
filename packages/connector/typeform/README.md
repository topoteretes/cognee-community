# Typeform Data-Source Connector for Cognee

The `cognee-community-connector-typeform` package allows you to sync Typeform forms, questions, and submitted customer responses directly into Cognee's AI memory graph and vector store.

---

## Installation

```bash
uv pip install cognee-community-connector-typeform
```

---

## Prerequisites & Authentication

1. Generate a Personal Access Token in your Typeform account under **Account Settings > Personal Tokens**.
2. Set your environment variable:

```bash
export TYPEFORM_API_KEY="your_personal_access_token"
```

---

## Usage Example

```python
import asyncio
import os
import cognee
from cognee_community_connector_typeform import typeform_source


async def main():
    # Ingest all visible forms and responses
    source = typeform_source()

    # Or restrict to specific forms:
    # source = typeform_source(form_ids=["form_abc123", "form_xyz456"])

    await cognee.remember(
        source,
        dataset_name="typeform_surveys",
    )

    # Query with semantic memory recall
    results = await cognee.recall(
        "What are the most common user complaints from recent onboarding surveys?",
        dataset_name="typeform_surveys",
    )
    print(results)


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Key Features

- **Document Ingestion (`DOCUMENT_SOURCE_ATTR`):** Normalizes complex structured form submissions into readable question-and-answer markdown documents for Cognee entity extraction.
- **Support for All Field Types:** Handles short/long text, multiple choice, matrix, ratings, dropdowns, dates, email, and file references.
- **Incremental Synchronization:** Supports `since` timestamp parameters to only ingest newly submitted responses.
- **Rate-Limit Resilience:** Automatic exponential backoff handling HTTP 429 and transient connection retries.
- **Full Offline Test Suite:** Verified using offline mocked Typeform REST fixtures.

---

## Running Tests

From the package directory:

```bash
uv run pytest
```
