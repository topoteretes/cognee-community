# cognee-community-connector-raindrop

A Raindrop.io data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync bookmarks, collections, and annotations into memory.

Addresses good-first-issue #4813.

## Install

```bash
uv pip install cognee-community-connector-raindrop
```

## Usage

```python
import cognee
from cognee_community_connector_raindrop import raindrop_source

await cognee.remember(
    raindrop_source(),  # RAINDROP_API_TOKEN from env
    dataset_name="raindrop",
)
```

## Setup

1. Create a test token at https://app.raindrop.io/settings/integrations
2. Set `RAINDROP_API_TOKEN`.

This is a starter implementation. Full lastUpdate-based incremental sync and optional page-content fetching should follow the Notion reference and Raindrop's API.
