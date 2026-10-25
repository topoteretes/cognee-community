# Workable Recruiting & ATS Community Connector for Cognee

An enterprise REST API connector integrating **Workable ATS & Recruiting** with **Cognee**. Ingest open job postings, role descriptions, required qualifications, and candidate pipeline stages into Cognee's cognitive knowledge and vector graph memory.

## Features

- **Hiring Requisitions & Requirements**: Ingests job titles, departments, employment types, locations, role descriptions, and required technical qualifications.
- **Candidate Pipeline Stages**: Tracks candidates across hiring stages (`applied`, `phone_screen`, `interview`, `offer`, `hired`, `disqualified`) with reasons.
- **Privacy First by Design**: Built-in candidate PII shielding (`include_candidate_pii=False` by default) ensures candidate identities and contact numbers remain masked from global vector memory.
- **Robust Error Handling**: Paces requests and handles 401 invalid API tokens, 403 access denials, and 429 rate limits gracefully.
- **Hermetic Pytest Suite**: 100% offline mock test suite covering job manifests, candidate pipelines, PII toggling, rate limits, and Markdown rendering.

## Installation

```bash
pip install cognee-community-connector-workable
```

## Quickstart

```python
import asyncio
from cognee_community_connector_workable import WorkableConnector

async def main():
    api_key = "your_workable_api_token"
    subdomain = "your-company-subdomain"

    connector = WorkableConnector(
        api_key=api_key,
        subdomain=subdomain,
        include_candidate_pii=False
    )

    # Ingest job requisitions and candidate pipelines into Cognee
    documents = await connector.to_documents(
        state="published",
        limit=25,
        include_candidates=True
    )

    # await cognee.add(documents)
    # await cognee.cognify()

    await connector.close()

if __name__ == "__main__":
    asyncio.run(main())
```

## Running Tests

Execute the hermetic unit tests offline:

```bash
pytest packages/connector/workable/tests/test_workable.py -v
```
