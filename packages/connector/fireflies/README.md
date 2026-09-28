# Fireflies connector

Sync Fireflies.ai meeting transcripts into cognee as speaker-aware documents.
The connector ingests selected transcript content, remembers a transcript-date
cursor between runs, and forgets meetings deleted upstream.

## Install

From this directory:

```bash
pip install -e .
```

Set your Fireflies API key:

```bash
export FIREFLIES_API_KEY="your-api-key"
```

PowerShell:

```powershell
$env:FIREFLIES_API_KEY = "your-api-key"
```

The connector sends the key as a Bearer token to
`https://api.fireflies.ai/graphql`.

## Use

```python
import cognee
from cognee_community_connector_fireflies import fireflies_source

source = fireflies_source(
    include_transcript=True,
    include_summary=True,
    include_action_items=True,
    include_speakers=True,
)

await cognee.remember(
    source,
    dataset_name="fireflies_meetings",
    primary_key="id",
    write_disposition="merge",
    max_rows_per_table=0,
)
await cognee.cognify(dataset_name="fireflies_meetings")
```

Each option controls whether that section is queried and included. Transcript
sentences retain speaker names, Fireflies speaker IDs, and timestamps; the
explicit wording is intended to help the normal cognify pipeline create people
and meeting relationships rather than anonymous flat text.

## Sync semantics

The first run lists every visible transcript and ingests it. Later runs keep
`last_date` and `known_ids` in dlt resource state:

- known transcripts at or before the cursor are skipped;
- unknown IDs are ingested even at the cursor boundary;
- IDs missing from a successful full listing emit dlt `_deleted` tombstones;
- a suspicious empty listing preserves prior state instead of deleting every meeting.

Fireflies defines `date` as transcript creation time. Consequently this cursor
detects newly-created transcripts, but not later edits whose creation timestamp
does not change. This is the provisional behavior requested by the issue.

Always call `cognee.remember` with `write_disposition="merge"` and
`max_rows_per_table=0`; deletion reconciliation needs the complete staged set.

## Example and tests

```bash
python examples/example.py
pytest tests/
```

Unit tests use fake clients and do not require a real Fireflies API key.
