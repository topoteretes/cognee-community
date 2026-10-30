# AtCoder Connector for Cognee

An open-source competitive programming connector to synchronize contest participation histories, problem submissions, execution performance, and ratings from **AtCoder** directly into **Cognee** knowledge graphs.

## Features
- **Submission History & Paradigms:** Tracks problem IDs, scores, execution durations, languages, and verdicts (AC, WA, TLE, MLE).
- **Contest Trajectory:** Ingests official contest ranks, performance ratings, and rating delta history.
- **Natural Language Engineering Queries:** Ask agents questions like *"Which Dynamic Programming problems did I fail during ABC contests?"* or *"Analyze my rating growth across the last 10 rounds"*.
- **Comprehensive Unit Testing:** Hermetic offline test suite with zero live credential requirement.

## Installation

```bash
pip install cognee-community-connector-atcoder
```

## Quick Start

```python
from cognee_community_connector_atcoder import AtCoderConnector

connector = AtCoderConnector(username="your_handle")
documents = connector.fetch_documents(include_contests=True)

for doc in documents:
    print(f"Ingested {doc['metadata']['type']}: {doc['metadata']}")
    print(doc["text"][:150])
```
