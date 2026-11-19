# Cognee Community Connector: Habitica

Ingest gamified habits, daily quests, and to-do tasks from **Habitica** into Cognee's AI Memory & Knowledge Graph.

## Features
- **Full Task Model Ingestion:** Ingests Habits, Dailies, To-Dos, and Rewards.
- **Detailed Gamified Attributes:** Extracts value scores, difficulty ratings, streak counters, checklists, and user tags.
- **Structured Markdown Generation:** Normalizes tasks into clean structured markdown ready for semantic vector search and entity relation mapping in Cognee.
- **Deterministic Deduplication:** Stable SHA-256 document hashing ensures idempotent synchronization.

## Installation
```bash
pip install -e packages/connector/habitica
```

## Quick Start
```python
from cognee_community_connector_habitica import habitica_source
import cognee

# Ingest all Habitica tasks
documents = habitica_source(
    user_id="your-habitica-user-uuid",
    api_token="your-habitica-api-token"
)

# Remember into Cognee Knowledge Graph
await cognee.remember(documents)
```

## Authentication
Find your API credentials under **Habitica Settings -> API**:
- `HABITICA_USER_ID`: User ID (UUID)
- `HABITICA_API_KEY`: API Token (UUID)

## Running Tests
```bash
PYTHONPATH=packages/connector/habitica pytest packages/connector/habitica/tests -v
```
