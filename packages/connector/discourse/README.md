# cognee-community-connector-discourse

A **Discourse** forum data-source connector for **cognee**: sync your community forum topics into memory — *"ask my forum"*.

It exposes a **dlt source** you hand to `cognee.remember(...)` /
`cognee.add(...)`. Discourse topics are exported as raw Markdown and ingested
as **normal documents** (they flow through cognee's cognify entity-extraction
pipeline, not the deterministic dlt-row path), via cognee's document-mode
marker.

## Requirements

Requires cognee ≥ 1.4.0 (document-mode support).
Works with any Discourse instance (public or private).

## Installation

```bash
pip install "cognee[discourse] @ git+https://github.com/topoteretes/cognee-community.git#subdirectory=packages/connector/discourse"
```

## Authentication

**Public forums**: No authentication required. Just provide the base URL.

**Private forums**: Create an API key in Admin → API → API Keys, then provide:

```bash
export DISCOURSE_BASE_URL="https://forum.example.com"
export DISCOURSE_API_KEY="your-api-key"       # optional, for private forums
export DISCOURSE_API_USERNAME="system"         # optional, for private forums
```

## Usage

```python
import cognee
from cognee_community_connector_discourse import discourse_source

# Configure cognee first

# Ingest all latest topics from a public forum
source = discourse_source(base_url="https://forum.example.com")
await cognee.add(source)
```

Or scope to specific categories:

```python
source = discourse_source(
    base_url="https://forum.example.com",
    category_ids=[3, 7],  # Only ingest from these categories
    tags=["announcement", "tutorial"],
)
```

## Sync model: full snapshot replace

This connector uses ``write_disposition="replace"`` — each sync produces the
complete current set of topics matching the configured scope. Anything dropped
from the listing (deleted, moved to a different category, archived) gets
cleaned up via cognee's orphan cleanup.

### Incremental optimization

While the disposition is ``replace`` (safest for correctness), the connector
**minimizes re-fetching** by:
1. Filtering the listing on ``bumped_at`` (last activity) via the cursor.
2. Only fetching full Markdown via ``/raw/{topic_id}`` for topics that appear
   in the current listing.
3. A 5-minute overlap window catches late-updating threads.

### Safety guarantee

> A listing API error **aborts the run before any Markdown is fetched and
> before staging is replaced**. A partial snapshot must never drive mass
> deletions. Individual ``/raw/{topic_id}`` fetch failures are logged as
> warnings and skipped — the topic is still discoverable by its title and
> metadata from the listing.

### Edge cases documented

- **Public vs private**: Public forums work with zero configuration beyond
  the base URL. Private forums require ``Api-Key`` + ``Api-Username`` headers.
  The connector auto-detects based on which environment variables are set.
- **Category scoping**: When ``category_ids`` are provided, the connector
  fetches from each category's ``/c/{id}/l/latest.json`` feed. Topics that
  appear in multiple categories are deduplicated by ID.
- **Archived topics**: Discourse's ``/latest.json`` by default includes
  archived topics. They are ingested with their current metadata.
- **Large forums**: Page pagination with 30 topics per request handles forums
  of any size. A safety cap of 100 pages prevents infinite loops.
- **Deleted posts**: The ``/raw/{topic_id}`` endpoint returns the current
  state of the topic. Deleted posts are typically redacted or removed from
  the raw output by Discourse itself.

## Data model (document-mode)

One document per Discourse topic containing:
- `id`, `category_id`, `slug`, `title`
- `tags` list
- `created_at`, `bumped_at` (last activity) timestamps
- `posts_count`, `views`, `like_count`
- Original poster username
- Full topic content exported as Markdown via `/raw/{topic_id}`
- Raw topic metadata preserved in the `raw` field

## Layout

```
packages/connector/discourse/
├── README.md
├── cognee_community_connector_discourse/
│   ├── __init__.py
│   └── discourse.py
├── examples/
│   └── example.py
├── tests/
│   ├── fixtures/           # Recorded API responses for offline tests
│   └── test_discourse.py
└── pyproject.toml
```

## Reference implementation

Read `packages/connector/notion/` first — it is the closest working
reference for the dlt + document-mode pattern this connector follows.
The sync model and edge-case documentation draw from lessons learned
reviewing high-quality Mergetober submissions.
