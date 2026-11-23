# cognee-community-connector-stackoverflow

A Stack Overflow data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync questions, answers, and accepted solutions into memory.

Addresses good-first-issue #4809.

## Install

```bash
uv pip install cognee-community-connector-stackoverflow
```

## Usage

```python
import cognee
from cognee_community_connector_stackoverflow import stackoverflow_source

await cognee.remember(
    stackoverflow_source(tags=["python", "machine-learning"]),
    dataset_name="stackoverflow",
)
```

## Setup

1. (Recommended) Register for an API key at https://stackapps.com/apps/oauth/register
2. Set `STACKOVERFLOW_API_KEY`.
3. Always pass one or more tags — daily quota is small without a key.

This is a starter implementation. Full fromdate-based incremental sync should follow the Notion reference and the Stack Exchange API.
