# cognee-community-connector-readwise

A Readwise data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync highlights, notes, and sources into memory.

Addresses good-first-issue #4812.

## Install

```bash
uv pip install cognee-community-connector-readwise
```

## Usage

```python
import cognee
from cognee_community_connector_readwise import readwise_source

await cognee.remember(
    readwise_source(),  # READWISE_API_TOKEN from env
    dataset_name="readwise",
)
```

## Setup

1. Get your access token at https://readwise.io/access_token
2. Set `READWISE_API_TOKEN`.

This is a starter implementation. Full updatedAfter-based incremental sync should follow the Notion reference and Readwise's export API. Highlights are pre-curated and high-signal.
