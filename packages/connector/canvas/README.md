# Cognee Community Connector: Canvas LMS

A `dlt` data-source connector for [cognee](https://github.com/topoteretes/cognee) that ingests Canvas LMS course pages, modules, assignments, and announcements directly into memory.

## Architecture

This connector implements the document ingestion path (`DOCUMENT_SOURCE_ATTR = "canvas"`):
* **Pages (`/pages`):** Course wiki pages with HTML rendered into clean plaintext and security-sensitive `verifier` URL query parameters automatically stripped.
* **Modules (`/modules`):** Hierarchical course structure and syllabus outlines detailing units, lessons, and items.
* **Assignments (`/assignments`):** Course assignments with due dates, unlock schedules, descriptions, and point values.
* **Announcements (`/discussion_topics?only_announcements=true`):** Instructor announcements and course updates.
* **Discussions (`/discussion_topics`):** Class discussion topics with opt-in author anonymization (`anonymize_authors=True`).

### Features

- **Link Header Pagination:** Handles Canvas's RFC 5988 `Link` pagination following `rel="next"` pointers without relying on inaccurate page counts.
- **Security & Privacy Protection:** File links containing `verifier=` parameters are sanitized to prevent unauthenticated access leaks. Student grades, submissions, user profiles, and inbox messages are strictly excluded.
- **Incremental Sync:** Tracks high-watermark updates per course and uses SHA-256 content hashing to ensure unchanged materials are never redundantly re-extracted.
- **Forget-on-Delete:** Configured with `write_disposition="merge"` emitting hard-delete `_deleted=True` tombstones when an item or course is deleted or unpublished.
- **Rate Limit Resilience:** Automatically slows down and retries on Canvas cost bucket depletion (`X-Rate-Limit-Remaining`, HTTP 429/403) and 5xx transient server responses.

## Setup & Token Generation

1. Log in to your Canvas instance (e.g. `https://canvas.instructure.com` or your institution URL).
2. Go to **Account** -> **Settings**.
3. Under **Approved Integrations**, click **+ New Access Token**.
4. Set the purpose (e.g. `cognee-memory`) and copy your generated token.

## Usage

```python
import asyncio
import cognee
from cognee_community_connector_canvas import canvas_source


async def main():
    source = canvas_source(
        access_token="canvas_access_token",
        base_url="https://canvas.instructure.com",
        resources=["pages", "modules", "assignments", "announcements"],
    )

    await cognee.remember(
        source,
        dataset_name="canvas",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )


if __name__ == "__main__":
    asyncio.run(main())
```

## Testing

```bash
cd packages/connector/canvas
uv sync
uv run pytest tests/
```
