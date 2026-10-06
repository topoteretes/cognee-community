# Cognee Community Connector: Calendly

A production-ready data-source connector for **Calendly** scheduled meetings, invitees, notes, and question responses, built for Cognee's AI knowledge graph and vector indexing engine.

## Overview

Calendly is one of the most widely used scheduling platforms for discovery calls, customer onboarding, user interviews, and team syncs. While calendar slots only show dates and times, **invitee question responses contain vital business context** (such as project goals, team sizes, agendas, and technical pain points).

This connector:
- Ingests scheduled events and attendee details as rich Markdown documents.
- Extracts and indexes invitee question responses and meeting notes.
- Uses **Document Mode (`cognify`)** (`DOCUMENT_SOURCE_ATTR = "calendly"`), ensuring meeting data flows through entity extraction and knowledge graph construction.
- Supports **Incremental Sync** via `min_start_time` watermarking stored in `dlt` state.
- Supports **Forget-on-Delete** via full snapshot reconciliation (`write_disposition="replace"`), so canceled or deleted meetings are purged by Cognee's `orphan_cleanup`.

---

## Authentication

Generate a **Personal Access Token** in Calendly:
1. Log in to your Calendly account.
2. Go to **Integrations** > **API & Webhooks**.
3. Under **Personal Access Tokens**, click **Generate New Token**.
4. Set the token in your environment:

```bash
export CALENDLY_API_KEY="your-calendly-personal-access-token"
```

You can also pass `api_key` directly to `calendly_source(api_key=...)`.

---

## Installation

```bash
pip install -e packages/connector/calendly
```

Or install with dependencies:

```bash
pip install "cognee==1.3.0" "dlt[sqlalchemy]>=1.9.0,<2" "httpx>=0.25.0,<1.0.0"
```

---

## Quickstart

```python
import asyncio
import os
import cognee
from cognee_community_connector_calendly import calendly_source


async def main():
    # 1. Initialize connector
    source = calendly_source(
        api_key=os.getenv("CALENDLY_API_KEY"),
        status="active",
        include_invitee_qa=True,
    )

    # 2. Add and cognify
    await cognee.add(source, dataset_name="calendly_meetings")
    await cognee.cognify(dataset_name="calendly_meetings")

    # 3. Query knowledge graph
    results = await cognee.search(
        search_type="INSIGHTS",
        query_text="What features did prospective clients ask about in demo calls?",
        dataset_name="calendly_meetings",
    )
    for res in results:
        print(res)


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Configuration Options

| Parameter | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `api_key` | `str \| None` | `None` | Personal Access Token or OAuth Bearer token (falls back to `CALENDLY_API_KEY` or `CALENDLY_ACCESS_TOKEN`). |
| `user_uri` | `str \| None` | `None` | Calendly user URI to scope events. If omitted, auto-discovers current authenticated user. |
| `organization_uri` | `str \| None` | `None` | Calendly organization URI to scope events across the entire organization. |
| `min_start_time` | `str \| None` | `None` | ISO8601 start cutoff. Used for incremental sync and watermarking. |
| `max_start_time` | `str \| None` | `None` | Optional ISO8601 upper bound cutoff. |
| `status` | `str` | `"active"` | Filter events by status (`active`, `canceled`). |
| `include_invitee_qa`| `bool` | `True` | Whether to fetch invitee question responses and notes for each event. |
| `client` | `CalendlyClient \| None` | `None` | Custom preconfigured `CalendlyClient` instance. |

---

## Incremental Sync & Forget-on-Delete

1. **Incremental Sync**: On subsequent runs, `calendly_source` reads `last_min_start_time` from `dlt.current.resource_state()` and queries only meetings starting at or after the high-watermark timestamp.
2. **Forget-on-Delete**: Staging uses `write_disposition="replace"`. Meetings canceled upstream drop out of the active snapshot, allowing Cognee's `orphan_cleanup` to prune dead graph nodes and vector embeddings.

---

## Testing

Run unit tests:

```bash
pytest packages/connector/calendly/tests/test_calendly.py -v
```
