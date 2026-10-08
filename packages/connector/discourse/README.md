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

## Data model (document-mode)

One document per Discourse topic containing:
- `id`, `category_id`, `slug`, `title`
- `tags` list
- `created_at`, `bumped_at` (last activity) timestamps
- `posts_count`, `views`, `like_count`
- Original poster username
- Full topic content exported as Markdown via `/raw/{topic_id}`
- Raw topic metadata preserved in the `raw` field

## Incremental sync

Uses the ``bumped_at`` field (last activity timestamp) from the topic
listing, stored in dlt state, with a 5-minute overlap window to catch
late-updating threads.

## Deletion

``write_disposition="replace"`` — each sync produces the complete current set
of topics; anything dropped from the listing gets cleaned up via cognee's
orphan cleanup.

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
│   └── test_discourse.py
└── pyproject.toml
```

## Reference implementation

Read `packages/connector/notion/` first — it is the closest working
reference for the dlt + document-mode pattern this connector follows.
