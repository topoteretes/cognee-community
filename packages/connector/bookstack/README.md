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

## Sync model: optimized replace with lazy re-fetch

This connector uses ``write_disposition="replace"`` for correctness, but
**avoids re-fetching unchanged page bodies**:

1. First, a **cheap listing** call fetches page IDs and `updated_at` timestamps
   (no body content).
2. Pages whose `updated_at` hasn't changed since the last sync are skipped.
3. Only new or updated pages have their Markdown body fetched via
   ``/api/pages/{id}/export-markdown``.

### Incremental sync

Uses the ``updated_at`` field via BookStack's ``filter[updated_at:gt]`` API
filter, stored in dlt state, with a 5-minute overlap window to catch
late-updating pages. Offset pagination handles large wikis.

### Safety guarantee

> A listing API error **aborts the run before any bodies are fetched and
> before staging is replaced**. A partial snapshot must never drive mass
> deletions. Individual page Markdown export failures are logged as warnings
> and skipped — the page is still discoverable by its metadata from the
> listing.

### Edge cases documented

- **Recycle bin**: Pages moved to the recycle bin disappear from the active
  listing and are cleaned up via orphan cleanup. The connector does NOT
  explicitly fetch deleted pages.
- **Draft pages**: BookStack's API by default returns only published pages.
  Drafts are not ingested unless the API token has "Manage all content"
  permissions and a custom filter is used.
- **Chapters**: Pages within chapters are ingested with their `chapter_id`
  preserved in metadata so the hierarchy is visible in the document.
- **Attachments**: File attachments and images referenced in Markdown are
  NOT fetched — only the page text is ingested. Image alt text and link
  titles remain in the Markdown.
- **Large wikis**: Offset pagination with 100 pages per request handles
  wikis of any size. The listing-first approach means we only pay the cost
  of full Markdown exports for pages that actually changed.

## Data model (document-mode)

One document per BookStack page containing:
- `id`, `book_id`, `chapter_id`, `slug`, `name`, `url`
- `created_at`, `updated_at` timestamps
- Book and chapter context
- Owner information
- Full page content exported as Markdown
- Raw page metadata preserved in the `raw` field

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
│   ├── fixtures/           # Recorded API responses for offline tests
│   └── test_bookstack.py
└── pyproject.toml
```

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

## Reference implementation

Read `packages/connector/notion/` first — it is the closest working
reference for the dlt + document-mode pattern this connector follows.
The sync model and edge-case documentation draw from lessons learned
reviewing high-quality Mergetober submissions.
