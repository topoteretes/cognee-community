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
A Sanity read-only API token + project ID.

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

## Sync model: full snapshot replace

This connector uses ``write_disposition="replace"`` — each sync produces the
complete current set of Sanity documents matching the configured filters.
Anything dropped from the GROQ query results (unpublished, deleted, filtered
out) gets cleaned up via cognee's orphan cleanup.

### Incremental optimization

While the disposition is ``replace`` (safest for correctness), the connector
**minimizes re-fetching** by filtering on ``_updatedAt`` in the GROQ query.
A 5-minute overlap window catches late-updating documents. Documents whose
content hash hasn't changed are not re-cognified by cognee.

### Safety guarantee

> A GROQ query error or API failure **aborts the run before staging is
> replaced**. A partial snapshot must never drive mass deletions. Only when
> the full query succeeds is the staging table rewritten. Transient errors
> (429, 5xx) are retried with exponential backoff; permanent errors (auth,
> invalid GROQ) raise immediately.

### Edge cases documented

- **Drafts**: By default, the connector filters for published documents only
  (``!(_id in path('drafts.**'))``). Drafts are not ingested unless the user
  explicitly provides a custom ``groq_filter`` that includes them.
- **Portable Text rendering**: Rich text blocks (``_type: "block"``) are
  extracted as prose. Non-text blocks (images, files, etc.) are skipped —
  their captions or alt text are included when available.
- **Reference fields**: References to other documents are rendered as
  ``[Reference: <_ref>]`` placeholders so relationships are visible in the
  text without requiring additional fetches.
- **Empty documents**: Documents with no extractable text fields still
  produce a minimal row with metadata; the text field carries the title and
  type information so the document is discoverable.
- **Large datasets**: The GROQ API returns up to 100 results per query. The
  connector paginates with ``start``/``limit`` parameters until all results
  are fetched.

## Data model (document-mode)

One document per Sanity CMS entry containing:
- `_id`, `_type`, `_createdAt`, `_updatedAt`, `_rev` metadata
- Extracted title (from common fields: `title`, `name`, `heading`, `headline`)
- Prose body extracted from Portable Text blocks (`body`, `content`, `description`, etc.)
- Raw document preserved in the `raw` field

## Acceptance criteria

- [x] A user connects via API token and selects what to ingest
- [x] Selected content is ingested and searchable in cognee
- [x] Incremental sync picks up only what changed since the last run
- [x] Deleting the source upstream removes it from the graph on the next sync
- [x] README with setup steps and a runnable example under `examples/`
- [x] Tests covering the ingest path and the incremental cursor

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
│   ├── fixtures/           # Recorded API responses for offline tests
│   └── test_sanity.py
└── pyproject.toml
```

## Reference implementation

Read `packages/connector/notion/` first — it is the closest working
reference for the dlt + document-mode pattern this connector follows.
The sync model and edge-case documentation draw from lessons learned
reviewing high-quality Mergetober submissions.
