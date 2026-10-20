# Todoist Connector for cognee

This is a `dlt` source that reads tasks, projects, and comments from Todoist.

## Usage

```python
import cognee
from cognee_community_connector_todoist import todoist_tasks

await cognee.remember(
    todoist_tasks(api_token="YOUR_TOKEN"),
    dataset_name="todoist",
    primary_key="id",
    write_disposition="merge",
    max_rows_per_table=0,
)
```
