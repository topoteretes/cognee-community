# 🎙️ Cognee Community Connector: Fireflies.ai

> **Sync your team's voice conversations, meeting decisions, and action items into persistent AI agent memory.**

The **Fireflies.ai connector** for [cognee](https://github.com/topoteretes/cognee) ingests meeting transcripts, executive summaries, action items, and speaker-attributed dialogues from [Fireflies.ai](https://fireflies.ai) directly into Cognee's knowledge graph and vector memory.

---

## 🌟 Why Fireflies + Cognee?

AI agents typically read documents (Notion, Google Drive) and code (GitHub), but **over 80% of real engineering and business decisions happen verbally in meetings**. Without meeting memory, agents suffer from "conversational amnesia".

This connector bridges that gap by transforming raw meeting speech into an interconnected knowledge graph:
* **True Speaker Attribution:** Maps speaker turns to verified participant identities (`Name (email)`), allowing the knowledge graph to link:
  * `[Alice (alice@example.com)] ──(committed to)──> [Action Item]`
  * `[Bob (bob@example.com)] ──(spoke about)──> [Architecture Migration]`
* **Document-Mode Routing:** Declares `DOCUMENT_SOURCE_ATTR = "fireflies"` so meeting notes flow through Cognee's GLiNER / LLM entity extraction rather than rigid tabular schemas.
* **Incremental Synchronization:** Uses a high-watermark timestamp cursor (`fromDate`) so re-runs only ingest new meetings.
* **Forget-on-Delete:** Automatically reconciles upstream deletions using `_deleted: True` tombstones on merge, triggering Cognee's `orphan_cleanup` to purge outdated nodes.
* **Rate-Limit Resilience:** Handles Fireflies plan quotas (e.g. 50 requests/day on Free tier) with exponential backoff and `Retry-After` header tracking.

---

## 📦 Installation

Install the connector within your Cognee environment:

```bash
pip install cognee-community-connector-fireflies
# Or using uv:
uv add cognee-community-connector-fireflies
```

---

## 🚀 Quickstart

### 1. Get your Fireflies API Key
Generate an API key from your [Fireflies Integrations Settings](https://app.fireflies.ai/integrations/api).

### 2. Run Ingestion

```python
import asyncio
import os
import cognee
from cognee_community_connector_fireflies import fireflies_source

DATASET_NAME = "team_meeting_memory"

async def main():
    # 1. Initialize the Fireflies source (defaults to FIREFLIES_API_KEY env var)
    source = fireflies_source(
        days_back=30,               # Backfill meetings from the last 30 days
        include_action_items=True,  # Ingest and attribute action items
        include_sentences=True,     # Ingest speaker-attributed transcript
    )

    # 2. Ingest into Cognee memory
    print("Ingesting meeting transcripts into Cognee knowledge graph...")
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",
    )

    # 3. Ask your AI agent about meeting commitments
    query = "Who committed to updating the rate-limit documentation in our sync?"
    result = await cognee.search(
        query_text=query,
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nRecall Result:\n", result)

if __name__ == "__main__":
    asyncio.run(main())
```

---

## ⚙️ Configuration & Parameters

The `fireflies_source(...)` factory accepts the following parameters:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `api_key` | `str \| None` | `None` | Fireflies API key. Falls back to `FIREFLIES_API_KEY` environment variable. |
| `days_back` | `int \| None` | `None` | Ingest meetings starting from N days ago on initial sync. |
| `limit` | `int` | `50` | Number of transcripts fetched per GraphQL page. |
| `include_action_items` | `bool` | `True` | Include formatted action items in synthesized document content. |
| `include_sentences` | `bool` | `True` | Include full speaker dialogue sentences in document content. |
| `endpoint` | `str` | `https://api.fireflies.ai/graphql` | Fireflies GraphQL endpoint URL. |

---

## 🧪 Testing

The test suite is **100% offline and mocked**, requiring no external API keys or live network calls:

```bash
# Run pytest from the package root:
pytest tests/ -v
```

All tests cover:
* Speaker attribution matching against attendee lists
* GraphQL query serialization and pagination
* Incremental cursor updates (`fromDate`)
* Forget-on-delete tombstone emission (`_deleted: True`)
* Safe sweep protection against empty responses
* HTTP 429 rate-limit backoff

---

## 📜 Developer Certificate of Origin (DCO)

All commits in this package are signed off in compliance with the Topoteretes Developer Certificate of Origin:

```bash
git commit -s -m "feat: Add Fireflies data-source connector"
```
