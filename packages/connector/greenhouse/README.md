# Cognee Community Connector: Greenhouse

A production-ready data-source connector for **Greenhouse** recruiting jobs, job posts, and candidate evaluation scorecards, built for Cognee's AI knowledge graph and vector indexing engine.

## Overview

Greenhouse is a leading hiring and applicant tracking system (ATS). Ingesting job requisitions, responsibilities, department requirements, and interview scorecard evaluations builds an organizational talent knowledge graph.

This connector:
- Ingests Greenhouse jobs and public job posts as rich Markdown documents.
- Includes a **Candidate Privacy Safeguard**: Interview evaluation scorecards are gated behind explicit opt-in (`include_interview_feedback=False` by default) and strip candidate contact PII (emails, phone numbers, addresses).
- Uses **Document Mode (`cognify`)** (`DOCUMENT_SOURCE_ATTR = "greenhouse"`), ensuring job roles, required skills, and evaluations flow through entity extraction and knowledge graph construction.
- Supports **Incremental Sync** via `updated_after` watermarking stored in persistent `dlt` state.
- Supports **Forget-on-Delete** via full snapshot reconciliation (`write_disposition="replace"`), so closed, archived, or deleted jobs and anonymized candidate feedback drop out of the snapshot and are purged by Cognee's `orphan_cleanup`.

---

## Authentication

Generate a **Harvest API Key** in Greenhouse:
1. Log in to your Greenhouse account as a Site Admin.
2. Go to **Configure** > **Dev Center** > **API Credential Management**.
3. Click **Create New API Key**, select **Harvest**, and check read permissions:
   - `Jobs` (GET)
   - `Job Posts` (GET)
   - `Scorecards` (GET, optional if syncing interview feedback)
4. Set the environment variable:

```bash
export GREENHOUSE_HARVEST_API_KEY="your-harvest-api-key"
```

Or pass `api_key` directly to `greenhouse_source(api_key=...)`.

---

## Installation

```bash
pip install -e packages/connector/greenhouse
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
from cognee_community_connector_greenhouse import greenhouse_source


async def main():
    source = greenhouse_source(
        api_key=os.getenv("GREENHOUSE_HARVEST_API_KEY"),
        job_status="open",
        include_job_posts=True,
        include_interview_feedback=False,  # Set to True to opt in to scorecards
    )

    await cognee.add(source, dataset_name="greenhouse_recruiting")
    await cognee.cognify(dataset_name="greenhouse_recruiting")

    results = await cognee.search(
        search_type="INSIGHTS",
        query_text="What qualifications and technical skills are required for backend roles?",
        dataset_name="greenhouse_recruiting",
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
| `api_key` | `str \| None` | `None` | Greenhouse Harvest API key (falls back to `GREENHOUSE_HARVEST_API_KEY`). |
| `updated_after` | `str \| None` | `None` | ISO8601 timestamp cutoff for updated records. |
| `created_after` | `str \| None` | `None` | Optional ISO8601 creation cutoff. |
| `job_status` | `str \| None` | `"open"` | Filter jobs by status (`open`, `closed`, or `None` for all). |
| `include_job_posts` | `bool` | `True` | Whether to fetch public job posts/descriptions. |
| `include_interview_feedback` | `bool` | `False` | **Explicit opt-in** to ingest interview scorecards and candidate evaluations. |
| `client` | `GreenhouseClient \| None` | `None` | Preconfigured `GreenhouseClient` instance. |

---

## Candidate Privacy & Data Retention

- **Opt-In Gate**: Interview scorecards and evaluations are candidate personal data. They are omitted by default and only fetched when `include_interview_feedback=True` is explicitly specified.
- **PII Scrubbing**: Scorecard ingestion strips candidate emails, phone numbers, and home addresses, preserving only competencies, evaluation questions, and hiring recommendations.

---

## Incremental Sync & Forget-on-Delete

1. **Incremental Sync**: The connector tracks `last_updated_after` in `dlt.current.resource_state()`, querying only records modified since the previous sync.
2. **Forget-on-Delete**: Staging uses `write_disposition="replace"`. Closed, filled, or deleted job openings and anonymized candidates drop out of the active snapshot, prompting Cognee's `orphan_cleanup` to purge stale nodes and embeddings.

---

## Testing

Run unit tests:

```bash
pytest packages/connector/greenhouse/tests/test_greenhouse.py -v
```
