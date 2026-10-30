# Clockify Connector for Cognee

An enterprise data connector to synchronize workspaces, client projects, and employee time logs from **Clockify** directly into **Cognee** knowledge graphs and semantic memory layers.

## Features
- **Comprehensive Project & Task Ingest:** Synchronizes billable status, project hierarchy, task descriptions, and duration metrics.
- **Natural Language Timeline Queries:** Enables LLMs and AI agents in Cognee to answer questions like *"What projects did the team focus on last sprint?"* and *"Calculate total engineering hours logged for Client X"*.
- **Pagination & Date Range Filtering:** Efficiently pulls time records across designated ISO date windows.
- **Hermetic Unit Test Suite:** Includes comprehensive test suites with zero external dependency on live Clockify credentials.

## Installation

```bash
pip install cognee-community-connector-clockify
```

## Quick Start

```python
from cognee_community_connector_clockify import ClockifyConnector

connector = ClockifyConnector(api_key="YOUR_CLOCKIFY_API_KEY")

# Extract time entries and project documents
documents = connector.fetch_documents(page_size=50)

for doc in documents:
    print(f"Log: {doc['metadata']['project_name']} - {doc['metadata']['entry_id']}")
    print(doc["text"])
```

## Running Tests

```bash
PYTHONPATH=packages/connector/clockify python3 -m pytest packages/connector/clockify/tests/ -v
```
