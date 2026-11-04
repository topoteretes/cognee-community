# Google Forms Connector for Cognee

A high-performance `dlt` data-source connector for [Cognee](https://github.com/topoteretes/cognee) that transforms **Google Forms schemas and respondent submissions** into structured memory documents for semantic and graph search.

Resolves **[topoteretes/cognee#5814](https://github.com/topoteretes/cognee/issues/5814)**.

---

## Capabilities

- **Form Schema & Structure Ingestion**: Ingests form title, description, and every question item (Short Answer, Paragraph, Multiple Choice, Linear Scale, Checkboxes) into a canonical schema document (`gform:{form_id}:schema`).
- **Response Document Normalization**: Maps raw answer arrays back to their human-readable question prompts (`gform:{form_id}:resp:{response_id}`).
- **Privacy First**: Respondent emails are excluded by default to protect PII, and only included when explicitly requested via `include_respondent_email=True`.
- **Incremental Sync**: Uses timestamp cursor tracking (`timestamp >= {cursor}`) stored in `dlt.current.resource_state()` to only pull new responses on subsequent syncs.
- **Forget-On-Delete**: Emits `_deleted: True` tombstones when forms are deleted (HTTP 404), ensuring Cognee's `orphan_cleanup` purges dead data from graph memory.

---

## Installation

```bash
pip install cognee-community-connector-google-forms
```

---

## Quickstart

```python
import asyncio
import cognee
from cognee_community_connector_google_forms import google_forms_source

async def main():
    # Ingest one or more Google Forms
    await cognee.remember(
        google_forms_source(
            form_ids=["1FAIpQLSc..."],
            include_respondent_email=False,
        ),
        dataset_name="customer_surveys",
        primary_key="id",
        write_disposition="merge",
    )

    # Ask questions across all customer feedback
    insights = await cognee.search(
        "What are the top three feature requests customers mentioned in the Q4 survey?"
    )
    print(insights)

if __name__ == "__main__":
    asyncio.run(main())
```

---

## Running Tests

The connector includes hermetic unit tests that execute without network access:

```bash
pytest packages/connector/google-forms/tests/ -v
```
