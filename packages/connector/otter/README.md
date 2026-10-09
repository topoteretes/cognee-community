# cognee-community-connector-otter

An **Otter.ai** data-source connector for **cognee**: sync your meeting transcripts into memory — *"ask my meetings"*.

It exposes a **dlt source** you hand to `cognee.remember(...)` /
`cognee.add(...)`. Otter.ai conversations are rendered as prose and ingested as
**normal documents** (they flow through cognee's cognify entity-extraction
pipeline, not the deterministic dlt-row path), via cognee's document-mode
marker.

## Requirements

Requires cognee ≥ 1.4.0 (document-mode support).
Requires an **Otter.ai Enterprise workspace** (Public API access).

## Installation

```bash
pip install "cognee[otter] @ git+https://github.com/topoteretes/cognee-community.git#subdirectory=packages/connector/otter"
```

## Authentication

1. In Otter.ai → Integrations → Developer tab
2. Click **Create key** and copy the API key
3. Provide via environment variable:

```bash
export OTTER_API_KEY="your-api-key"
```

## Usage

```python
import cognee
from cognee_community_connector_otter import otter_source

# Configure cognee first

# Ingest all conversations (most recent first, incremental)
source = otter_source(include_shared=False)
await cognee.add(source)
```

## Sync model: full snapshot replace

This connector uses ``write_disposition="replace"`` — each sync produces the
complete current set of conversations visible to the API key. Anything dropped
from the listing (deleted, access revoked) gets cleaned up via cognee's orphan
cleanup.

### Incremental optimization

While the disposition is ``replace`` (safest for correctness), the connector
**minimizes re-fetching** by tracking the ``created_at`` cursor in dlt state.
A 5-minute overlap window catches late-processed meetings. The Otter.ai API
returns conversations in pages of 100 via cursor pagination.

### Safety guarantee

> An API error **aborts the run before staging is replaced**. A partial
> snapshot must never drive mass deletions. Only when all pages have been
> successfully fetched and all transcripts retrieved is the staging table
> rewritten. Transient errors (429, 5xx) are retried with exponential backoff
> honoring the ``Retry-After`` header; permanent errors (401 invalid key)
> raise immediately.

### Edge cases documented

- **Shared meetings**: By default, ``include_shared=False`` — only meetings
  owned by the API key's user are ingested. Set ``include_shared=True`` to
  also ingest meetings shared with the user. Note: shared meetings may have
  different access permissions.
- **In-progress meetings**: Otter.ai's API may return meetings that are still
  being recorded. These have partial transcripts. The connector ingests them
  as-is; they will be updated on the next sync when processing completes.
- **Large transcripts**: Very long meetings (2+ hours) produce large text
  bodies. The connector passes them through unchanged; cognee's chunking
  handles them during ingestion.
- **Speaker identification**: Speaker labels are preserved as provided by the
  API (e.g., "Speaker 0", "Speaker 1"). If Otter.ai has identified speakers,
  those names appear in the text.
- **Transcript fetch failures**: If fetching an individual transcript fails
  (network error), the connector logs a warning and continues with the
  conversation metadata only. The meeting is still discoverable by title and
  participants.

## Data model (document-mode)

One document per Otter.ai conversation containing:
- `id`, `title`, `url`, `created_at` metadata
- Owner name + email
- `abstract_summary` (AI-generated meeting summary)
- Full transcript text with speaker labels and timestamps
- Meeting participants list
- Raw conversation preserved in the `raw` field

## Layout

```
packages/connector/otter/
├── README.md
├── cognee_community_connector_otter/
│   ├── __init__.py
│   └── otter.py
├── examples/
│   └── example.py
├── tests/
│   ├── fixtures/           # Recorded API responses for offline tests
│   └── test_otter.py
└── pyproject.toml
```

## Reference implementation

Read `packages/connector/notion/` first — it is the closest working
reference for the dlt + document-mode pattern this connector follows.
The sync model and edge-case documentation draw from lessons learned
reviewing high-quality Mergetober submissions.
