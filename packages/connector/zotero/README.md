# Zotero Connector for cognee

This is a `dlt` source that reads library items from a Zotero user or group library.

## Usage

```python
import cognee
from cognee_community_connector_zotero import zotero_items

await cognee.remember(
    zotero_items(api_key="YOUR_ZOTERO_API_KEY", user_id="YOUR_USER_ID"),
    dataset_name="zotero",
    primary_key="id",
    write_disposition="replace",
    max_rows_per_table=0,
)
```
