# cognee-community-connector-zotero

A Zotero data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync references, notes, and attachment text into memory.

Addresses good-first-issue #4814.

## Install

```bash
uv pip install cognee-community-connector-zotero
```

## Usage

```python
import cognee
from cognee_community_connector_zotero import zotero_source

await cognee.remember(
    zotero_source(),  # ZOTERO_API_KEY + ZOTERO_LIBRARY_ID from env
    dataset_name="zotero",
)
```

## Setup

1. Create an API key at https://www.zotero.org/settings/keys
2. Set `ZOTERO_API_KEY` and `ZOTERO_LIBRARY_ID` (your user ID).

This is a starter implementation. Full version-based incremental sync and note/attachment extraction should follow the Notion reference and Zotero's REST API.
