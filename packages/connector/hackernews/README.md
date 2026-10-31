# cognee-community-connector-hackernews

Hacker News data-source connector for [cognee](https://github.com/topoteretes/cognee).

Ingests top stories, Ask HN, Show HN discussions, and community comments from Hacker News into cognee memory.

## Usage

```python
import cognee
from cognee_community_connector_hackernews import hackernews_source

# Ingest top 30 stories with high-signal comments
await cognee.remember(
    hackernews_source(story_type="top", limit=30, include_top_comments=3),
    dataset_name="hacker-news",
)
```

## Features

- **Discussion feeds**: Supports `top`, `best`, `new`, `ask`, and `show` story categories.
- **Comment enrichment**: Automatically extracts and embeds top discussion replies for deep context.
- **Clean HTML formatting**: Cleans markup into human-readable and LLM-friendly markdown.
- **Stable IDs**: Deterministic item IDs prevent entity duplicates across runs.
