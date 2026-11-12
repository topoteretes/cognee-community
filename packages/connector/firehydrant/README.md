# FireHydrant Connector for Cognee

A production data-source connector for [Cognee](https://github.com/topoteretes/cognee) that ingests incidents, timeline updates, impacted infrastructure services, environments, runbooks, and retrospective reports from [FireHydrant](https://firehydrant.com/) into Cognee's persistent spatial knowledge graph and vector memory.

Resolves **[cognee#6107](https://github.com/topoteretes/cognee/issues/6107)**.

---

## Capabilities

- **Incident & Retrospective Ingestion**: Maps incident severities, milestones, impacted services, environments, summaries, and timeline notes into structured Markdown.
- **Node-Set Graph Traversal**: Generates node-set graph connections for `incident`, `severity`, `priority`, `service`, `environment`, and `milestone` (e.g. `service → incident → retrospective`) so on-call AI agents can answer questions like *"Have we had an outage in the billing-worker before, and what fixed it?"*.
- **Incremental Sync**: Supports incremental sync through `updated_after` timestamp filtering.
- **Resilience & Rate-Limiting**: Graceful backoff retry on HTTP 429 rate limits matching FireHydrant's 300 req/min budget.
- **Hermetic Unit Tests**: Fully covered with isolated unit tests without network dependencies.

---

## Installation

```bash
pip install cognee-community-connector-firehydrant
```

---

## Configuration

| Variable | Description | Default |
| :--- | :--- | :--- |
| `FIREHYDRANT_API_KEY` | Bot or user API token from FireHydrant | Required |
| `FIREHYDRANT_BASE_URL` | Base API URL | `https://api.firehydrant.io/v1` |

---

## Quickstart

```python
import cognee
from cognee_community_connector_firehydrant import firehydrant_data

# Ingest incidents into Cognee memory
for incident_doc in firehydrant_data(
    api_token="your_bot_token",
    limit=50
):
    print(f"Ingesting incident: {incident_doc['id']}")
    # cognee.add(incident_doc["text"], dataset_name="firehydrant_incidents")

# Build knowledge graph links
# cognee.cognify(dataset_name="firehydrant_incidents")
```

---

## Unit Testing

```bash
PYTHONPATH=. pytest tests/ -v
```
