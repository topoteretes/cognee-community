# cognee-community-connector-evernote

Evernote data-source connector for cognee.

Extracts notes, notebooks, and tags from Evernote into cognee memory with
incremental synchronization via Evernote sync chunks and forget-on-delete.

## Installation

```bash
pip install cognee-community-connector-evernote
```

## Usage

```python
from cognee_community_connector_evernote import evernote_source

source = evernote_source(token="your-evernote-token")
```

Set `EVERNOTE_TOKEN` environment variable as an alternative to passing `token=`.
