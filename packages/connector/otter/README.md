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

## Data model (document-mode)

One document per Otter.ai conversation containing:
- `id`, `title`, `url`, `created_at` metadata
- Owner name + email
- `abstract_summary` (AI-generated meeting summary)
- Full transcript text with speaker labels and timestamps
- Meeting participants list
- Raw conversation preserved in the `raw` field

## Incremental sync

Uses the ``created_at`` field from the conversations listing, stored in dlt
state, with a 5-minute overlap window to catch late-processed meetings.

## Deletion

``write_disposition="replace"`` — each sync produces the complete current set
of conversations; anything dropped from the listing gets cleaned up via
cognee's orphan cleanup.

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
│   └── test_otter.py
└── pyproject.toml
```

## Reference implementation

Read `packages/connector/notion/` first — it is the closest working
reference for the dlt + document-mode pattern this connector follows.
