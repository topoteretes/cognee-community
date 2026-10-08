# cognee-community-connector-bookstack

A **BookStack** wiki data-source connector for **cognee**: sync your documentation pages into memory — *"ask my wiki"*.

It exposes a **dlt source** you hand to `cognee.remember(...)` /
`cognee.add(...)`. BookStack pages are exported as Markdown and ingested as
**normal documents** (they flow through cognee's cognify entity-extraction
pipeline, not the deterministic dlt-row path), via cognee's document-mode
marker.

## Requirements

Requires cognee ≥ 1.4.0 (document-mode support).
Requires a BookStack instance (self-hosted or SaaS) with API access enabled.

## Installation

```bash
pip install "cognee[bookstack] @ git+https://github.com/topoteretes/cognee-community.git#subdirectory=packages/connector/bookstack"
```

## Authentication

1. In BookStack → Settings → API Tokens → Create Token
2. Copy the **Token ID** and **Token Secret**
3. Provide credentials and your instance URL:

```bash
export BOOKSTACK_BASE_URL="https://your-bookstack.example.com"
export BOOKSTACK_TOKEN_ID="your-token-id"
export BOOKSTACK_TOKEN_SECRET="your-token-secret"
```

## Usage

```python
import cognee
from cognee_community_connector_bookstack import bookstack_source

# Configure cognee first

# Ingest all pages (incremental after first run)
source = bookstack_source()
await cognee.add(source)
```

Or scope to specific shelves/books:

```python
source = bookstack_source(
    shelf_ids=[5, 8],  # Only ingest from these shelves
)
```

## Data model (document-mode)

One document per BookStack page containing:
- `id`, `book_id`, `chapter_id`, `slug`, `name`, `url`
- `created_at`, `updated_at` timestamps
- Book and chapter context
- Owner information
- Full page content exported as Markdown
- Raw page metadata preserved in the `raw` field

## Incremental sync

Uses the ``updated_at`` field via BookStack's ``filter[updated_at:gt]`` API
filter, stored in dlt state, with a 5-minute overlap window to catch
late-updating pages.

## Deletion

Each run does a cheap ID-only listing. Pages that disappear from the listing
(deleted, moved to recycle bin) get cleaned up via cognee's orphan cleanup.
Page bodies are only re-fetched when ``updated_at`` changes.

## Layout

```
packages/connector/bookstack/
├── README.md
├── cognee_community_connector_bookstack/
│   ├── __init__.py
│   └── bookstack.py
├── examples/
│   └── example.py
├── tests/
│   └── test_bookstack.py
└── pyproject.toml
```

## Reference implementation

Read `packages/connector/notion/` first — it is the closest working
reference for the dlt + document-mode pattern this connector follows.

## Testing with Docker

BookStack is easy to self-host for testing:

```bash
docker run -d --name bookstack-test \
  -p 8080:80 \
  -e APP_URL=http://localhost:8080 \
  -e DB_HOST=bookstack-db \
  --link bookstack-db:mysql \
  lscr.io/linuxserver/bookstack:latest
```

Then create an API token in Settings → API Tokens.
