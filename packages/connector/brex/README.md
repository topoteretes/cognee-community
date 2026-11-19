# Brex Connector for cognee
    
This is a `dlt` source that reads expenses and budgets from Brex using the Brex API.

## Usage

```python
import cognee
from cognee_community_connector_brex import brex_expenses

await cognee.remember(
    brex_expenses(api_token="YOUR_BREX_TOKEN"),
    dataset_name="brex",
    primary_key="id",
    write_disposition="replace",
    max_rows_per_table=0,
)
```
