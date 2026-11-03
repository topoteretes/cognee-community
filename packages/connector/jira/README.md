# Cognee Community Connector: Atlassian Jira

A community data-source connector for [Cognee](https://github.com/topoteretes/cognee) that ingests **Atlassian Jira** project issues, agile sprint statuses, priority tags, and engineering comments into the Cognee graph and vector memory.

Resolves: [`cognee#6466`](https://github.com/topoteretes/cognee/issues/6466).

---

## Features

- **Rich Issue Context:** Ingests issue key (e.g. `PROJ-101`), summary, issue type, priority, status, project name, assignee, reporter, and labels.
- **Atlassian Document Format (ADF) Parser:** Recursively decodes Atlassian Document Format JSON into clean readable Markdown/text.
- **Conversation Discussion Extraction:** Ingests full issue comment histories, preserving author timestamps and dialogue.
- **Incremental JQL Sync:** Filters with `updated >= 'timestamp'` to ingest only modified tickets on subsequent syncs.
- **Cognify Ready:** Formats clean Markdown documents structured for semantic search and Q&A (e.g. *"Which high-priority bugs are unassigned in Project X?"*).
- **Flexible Auth:** Supports Jira API token (`email:api_token`) or Personal Access Tokens (Bearer).

---

## Installation

```bash
pip install cognee-community-connector-jira
```

---

## Usage

```python
import cognee
from cognee_community_connector_jira import jira_source

# Ingest data from your Atlassian Jira Cloud instance
cognee.add(jira_source(domain="acme-corp", projects=["INFRA", "CORE"]))
cognee.cognify()

# Ask questions about active engineering tickets
results = cognee.search("What did we decide about the auth architecture in the last sprint?")
```
