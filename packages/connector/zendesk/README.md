# Cognee Community Connector: Zendesk

A community data-source connector for [Cognee](https://github.com/topoteretes/cognee) that ingests **Zendesk** customer support tickets, conversation threads, user profiles, priority tags, and resolution histories into the Cognee graph and vector memory.

Resolves: [`cognee#6465`](https://github.com/topoteretes/cognee/issues/6465).

---

## Features

- **Rich Ticket Context:** Ingests ticket subject, problem description, status (New, Open, Pending, Solved, Closed), priority, requester, assignee, and tags.
- **Conversation Thread Extraction:** Parses full ticket comment histories, maintaining distinction between public customer replies and internal agent notes.
- **Incremental Synchronization:** Supports `start_time` epoch cursor to ingest only recently created or updated tickets.
- **Cognify Ready:** Produces formatted Markdown documents ready for semantic search, customer sentiment analysis, and analytical question answering (e.g. *"What are the most common billing issues this week?"*).
- **Flexible Auth:** Supports Zendesk API token (`email/token:api_token`) or OAuth2 bearer tokens.

---

## Installation

```bash
pip install cognee-community-connector-zendesk
```

---

## Usage

```python
import cognee
from cognee_community_connector_zendesk import zendesk_source

# Ingest data from your Zendesk subdomain
cognee.add(zendesk_source(subdomain="acme-help"))
cognee.cognify()

# Query customer support intelligence
results = cognee.search("Which enterprise accounts have unresolved high-priority bugs?")
```
