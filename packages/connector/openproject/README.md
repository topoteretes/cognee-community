# cognee-community-connector-openproject

An **OpenProject** data-source connector for **cognee**: sync your work packages, status, priorities, and team discussions into memory — *"ask my project management"*.

It exposes a **dlt source** you hand to `cognee.remember(...)` / `cognee.add(...)`.
OpenProject work packages are rendered as prose and ingested as **normal documents**
(they flow through cognee's cognify entity-extraction pipeline, not the
deterministic dlt-row path), via cognee's document-mode marker.

## Requirements

Requires cognee ≥ 1.4.0 (document-mode support).
An OpenProject instance (self-hosted or Cloud) with API v3 access enabled.

## Installation

```bash
pip install "cognee[openproject] @ git+https://github.com/topoteretes/cognee-community.git#subdirectory=packages/connector/openproject"
```

## Authentication

1. In OpenProject → My Account → Access Tokens → API → Generate token
2. Copy the API key
3. Provide credentials and your instance URL:

```bash
export OPENPROJECT_BASE_URL="https://openproject.example.com"
export OPENPROJECT_API_KEY="your-api-key"
```

## Usage

```python
import cognee
from cognee_community_connector_openproject import openproject_source

# Configure cognee first

# Ingest all visible work packages
source = openproject_source()
await cognee.add(source)
```

Or scope to specific projects:

```python
source = openproject_source(
    base_url="https://openproject.example.com",
    project_ids=[5, 12],  # Only ingest from these projects
)
```

## Sync model: merge + _deleted tombstones

This connector uses ``write_disposition="merge"`` with a ``_deleted`` boolean column
(rather than a full-snapshot ``replace``) so incremental syncs are cheap and safe.

Each run performs three phases:

1. **Re-check active items**: Work packages in active statuses (in progress, new,
   specified, etc.) are fetched individually. Their state may have advanced
   (completed, new comments, description edited).
2. **Incremental fetch**: ``GET /api/v3/work_packages`` with ``updatedAt`` filter
   pulls anything changed since the last sync (minus a 5-minute overlap window to
   catch late-updating items). Page-based pagination handles large projects.
3. **Deletion detection** (first-run only): On the initial full sync, work packages
   that were previously known but no longer appear in the active listing receive a
   ``_deleted=True`` tombstone. cognee's ingestion pipeline removes these from the graph.

### Safety guarantee

> A transient API error **aborts the run before any tombstone is emitted**.
> A partial snapshot must never drive mass deletions. Permanent errors (401/403 on
> the root endpoint) raise immediately. A 404 on an individual active work package
> is treated as "gone" and tombstoned safely.

### Edge cases documented

- **Comments trigger re-sync**: Adding a comment to a work package updates its
  ``updatedAt`` timestamp, so the incremental filter picks it up automatically.
- **Active status re-check**: Work packages in "in progress", "new", "specified",
  "confirmed", "scheduled", or "design" status are tracked and re-checked on each
  run until they reach a terminal status (closed, done, rejected, on hold).
- **Pagination**: OpenProject uses page-number-based pagination (``offset`` is a
  page number, not a row offset). The connector reads all pages until reaching
  ``total``.
- **Wiki pages**: API v3 cannot list wiki pages of a project (only read by ID).
  Version 1 focuses on work packages and comments only.
- **Custom fields**: Custom field values are preserved in the ``raw`` field but
  not extracted into the prose body. The ``description`` field captures the main
  narrative.
- **Large attachments**: Attachments are not fetched — only the work package
  text, metadata, and comment count are ingested.

## Data model (document-mode)

One document per OpenProject work package containing:
- `id`, `project_id`, `type`, `status`, `priority`
- `subject`, `description` (raw text)
- `assignee`, `version`, `author`
- `created_at`, `updated_at`, `due_date`, `start_date`
- `estimated_time`, `comment_count`
- Full raw work package preserved in the `raw` field
- `_deleted` tombstone flag

## Testing with Docker

OpenProject is easy to self-host for testing:

```bash
docker run -d --name openproject-test \
  -p 8080:80 \
  -e OPENPROJECT_SECRET_KEY_BASE=test \
  openproject/openproject:15
```

Then create an API token in My Account → Access Tokens.

## Layout

```
packages/connector/openproject/
├── README.md
├── cognee_community_connector_openproject/
│   ├── __init__.py
│   └── openproject.py
├── examples/
│   └── example.py
├── tests/
│   ├── fixtures/           # Recorded API responses for offline tests
│   └── test_openproject.py
└── pyproject.toml
```

## Reference implementation

Read `packages/connector/notion/` first for the document-mode pattern. This
connector extends that pattern with ``merge`` + ``_deleted`` tombstones,
active-item re-checking, and page-based pagination — advanced patterns learned
reviewing high-quality Mergetober submissions.
