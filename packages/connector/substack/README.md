# cognee-community-connector-substack

A Substack data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync newsletter posts into memory via RSS.

Addresses good-first-issue #4810.

## Install

```bash
uv pip install cognee-community-connector-substack
```

## Usage

```python
import cognee
from cognee_community_connector_substack import substack_source

await cognee.remember(
    substack_source(feed_url="https://example.substack.com/feed"),
    dataset_name="substack",
)
```

## Setup

Set `SUBSTACK_FEED_URL` to a public Substack RSS feed (or pass it directly). No API key needed.

This is a starter implementation. Full RSS parsing and pubDate-based incremental sync should follow the Notion reference.
