# Cognee PagerDuty Community Connector

[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A community connector for [Cognee](https://github.com/topoteretes/cognee) that ingests **PagerDuty** incidents, active services, and responder metadata into Cognee's AI Memory and Knowledge Graph.

## Features

- **Incident Ingestion:** Ingest incident titles, status (`triggered`, `acknowledged`, `resolved`), urgency levels (`high`, `low`), and assignees.
- **Service Catalog Mapping:** Ingest operational service health states and map relationships between microservices and their corresponding outages.
- **Graph RAG Optimized:** Synthesizes structured Markdown with exact entity relationships, enabling Cognee to answer complex on-call queries like:
  - *"Who was on call when the payment gateway degraded?"*
  - *"Which services suffered the most high-urgency alerts this quarter?"*
- **Offline & Mock Friendly:** Built for deterministic entity hashing and verified with offline unit tests.

## Installation

```bash
pip install cognee-community-connector-pagerduty
```

Or install from source:

```bash
git clone https://github.com/bhomvratrai-glitch/cognee-community.git
cd cognee-community/packages/connector/pagerduty
pip install -e .
```

## Quickstart

```python
import os
from cognee_community_connector_pagerduty import PagerDutyConnector

# Initialize connector
connector = PagerDutyConnector(
    api_token=os.environ["PAGERDUTY_API_TOKEN"],
    statuses=["triggered", "acknowledged"],
    urgencies=["high"],
    include_services=True
)

# Fetch documents for Cognee
documents = connector.fetch_documents(limit=25)
print(f"Retrieved {len(documents)} incident and service documents for Cognee ingestion.")
```

## Running Tests

```bash
pytest tests/
```
