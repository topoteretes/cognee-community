# Cognee Community Fathom Connector

Connect Fathom meeting data to a DLT pipeline for use with Cognee.

## Features

- API-key authentication using the `X-Api-Key` header.
- Paginated meeting retrieval using `next_cursor`.
- Normalized meeting titles, invitee emails, summaries, and action items.
- Optional transcript retrieval with speaker attribution, when supplied by the API.
- Incremental retrieval with `created_after`.

> **Limitations:** Deleted-meeting reconciliation and automatic re-checking of recently updated action items are not yet implemented.

## Requirements

- Python 3.10 or newer
- A Fathom API key with access to the external API

## Installation

From the repository root:

```powershell
python -m pip install -e .\packages\connector\fathom
```

## Quick start

```python
import os
import dlt

from cognee_community_connector_fathom import fathom_source

pipeline = dlt.pipeline(
    pipeline_name="fathom_meetings",
    destination="duckdb",
    dataset_name="fathom_data",
)

source = fathom_source(
    api_key=os.environ["FATHOM_API_KEY"],
    include_transcripts=False,
)

info = pipeline.run(source)
print(info)
```

Set `FATHOM_API_KEY` in your environment before running the example.

For incremental retrieval, pass `created_after` as the date/time format accepted by the Fathom API. For transcript ingestion, set `include_transcripts=True`; transcripts may substantially increase the amount of data retrieved.

## Tests

Run from this connector directory:

```powershell
python -m pytest tests -q
```

## Privacy

Invitee emails are stored as structured meeting attributes. Handle them as personal data and avoid including them in document text by default.

## Current scope

This package currently retrieves and normalizes meeting data into a DLT resource. End-to-end graph ingestion and deletion reconciliation must be verified and implemented before claiming full support for those behaviors.
