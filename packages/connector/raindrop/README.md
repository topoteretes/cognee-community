# Raindrop Connector for cognee
    
This is a `dlt` source that reads bookmarks from Raindrop.io using the Raindrop API.

## Usage

```python
import cognee
from cognee_community_connector_raindrop import raindrop_bookmarks

await cognee.remember(
    raindrop_bookmarks(api_token="YOUR_RAINDROP_TOKEN"),
    dataset_name="raindrop",
    primary_key="id",
    write_disposition="merge",
    max_rows_per_table=0,
)
```
