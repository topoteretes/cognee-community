# Readwise Connector for cognee

This is a `dlt` source that reads highlights and books from the Readwise API.

## Usage

```python
import cognee
from cognee_community_connector_readwise import readwise_highlights

await cognee.remember(
    readwise_highlights(api_token="YOUR_READWISE_TOKEN"),
    dataset_name="readwise",
    primary_key="id",
    write_disposition="replace",
    max_rows_per_table=0,
)
```
