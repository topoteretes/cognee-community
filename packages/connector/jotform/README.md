# Jotform Form Builder Community Connector for Cognee

An enterprise REST API connector integrating **Jotform** with **Cognee**. Ingest online form schemas, map field IDs back to question labels, and ingest user submissions into Cognee's cognitive knowledge and vector graph memory.

## Features

- **Form Schema & Label Mapping**: Dynamically fetches form questions to resolve field IDs (e.g. `q2`) back into human-readable question prompts (e.g. `Property Address`).
- **Submission Document Ingestion**: Converts form entries into structured Markdown documents optimized for LLM reasoning and entity extraction.
- **Data Minimization & Sensitive Field Exclusions**: Built-in privacy guard (`exclude_payment_fields=True` by default) automatically strips payment integration widgets and transaction credentials from memory graphs.
- **Robust Error Handling**: Handles 401 invalid API keys, 403 form access denials, and 429 daily rate limits gracefully.
- **Hermetic Pytest Suite**: 100% offline mock test suite covering form schemas, submission label mapping, sensitive exclusions, and rate limits.

## Installation

```bash
pip install cognee-community-connector-jotform
```

## Quickstart

```python
import asyncio
from cognee_community_connector_jotform import JotformConnector

async def main():
    api_key = "your_jotform_api_key"

    connector = JotformConnector(
        api_key=api_key,
        exclude_payment_fields=True
    )

    # Ingest form overviews and submissions into Cognee
    documents = await connector.to_documents(limit_per_form=25)

    # await cognee.add(documents)
    # await cognee.cognify()

    await connector.close()

if __name__ == "__main__":
    asyncio.run(main())
```

## Running Tests

Execute the hermetic unit tests offline:

```bash
pytest packages/connector/jotform/tests/test_jotform.py -v
```
