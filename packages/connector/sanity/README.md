# cognee-community-connector-sanity

A **Sanity CMS** data-source connector for **cognee**: sync your structured CMS
content into memory — *"ask my content"*.

It exposes a **dlt source** you hand to `cognee.remember(...)` /
`cognee.add(...)`. Sanity documents are rendered as prose and ingested as
**normal documents** (they flow through cognee's cognify entity-extraction
pipeline, not the deterministic dlt-row path), via cognee's document-mode
marker.

## Requirements

Requires cognee ≥ 1.4.0 (document-mode support).

## Installation

```bash
pip install "cognee[sanity] @ git+https://github.com/topoteretes/cognee-community.git#subdirectory=packages/connector/sanity"
```

## Authentication

1. In Sanity Studio → API → Add API token (read-only)
2. Find your **project ID** (under Project settings)
3. Provide credentials:

```bash
export SANITY_PROJECT_ID="your-project-id"
export SANITY_API_TOKEN="your-read-only-token"
```

## Usage

```python
import cognee
from cognee_community_connector_sanity import sanity_source

# Configure cognee first

# Ingest all published documents
source = sanity_source(
    dataset="production",
    document_types=["post", "page", "article"],  # optional: restrict to types
    groq_filter="status == 'published'",  # optional: extra GROQ filter
)

await cognee.add(source)
```

## Data model (document-mode)

One document per Sanity CMS entry containing:
- `_id`, `_type`, `_createdAt`, `_updatedAt`, `_rev` metadata
- Extracted title (from common fields: `title`, `name`, `heading`, `headline`)
- Prose body extracted from Portable Text blocks (`body`, `content`, `description`, etc.)
- Raw document preserved in the `raw` field

## Incremental sync

Uses the ``_updatedAt`` field in GROQ filters, stored in dlt state, with a
5-minute overlap window to catch late-updating documents.

## Deletion

``write_disposition="replace"`` — each sync produces the complete current set
of documents matching the configured filters; anything dropped from the query
results gets cleaned up via cognee's orphan cleanup.

## Acceptance criteria

- [ ] A user connects via API token and selects what to ingest
- [ ] Selected content is ingested and searchable in cognee
- [ ] Incremental sync picks up only what changed since the last run
- [ ] Deleting the source upstream removes it from the graph on the next sync
- [ ] README with setup steps and a runnable example under `examples/`
- [ ] Tests covering the ingest path and the incremental cursor

## Layout

```
packages/connector/sanity/
├── README.md
├── cognee_community_connector_sanity/
│   ├── __init__.py
│   └── sanity.py
├── examples/
│   └── example.py
├── tests/
│   └── test_sanity.py
└── pyproject.toml
```

## Reference implementation

Read `packages/connector/notion/` first — it is the closest working
reference for the dlt + document-mode pattern this connector follows.
