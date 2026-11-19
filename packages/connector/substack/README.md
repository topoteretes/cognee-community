# Substack Connector for cognee

This is a `dlt` source that reads posts from a Substack publication using its public API.

## Usage

```python
import cognee
from cognee_community_connector_substack import substack_posts

await cognee.remember(
    substack_posts(subdomain="your-publication"),
    dataset_name="substack",
    primary_key="id",
    write_disposition="replace",
    max_rows_per_table=0,
)
```
