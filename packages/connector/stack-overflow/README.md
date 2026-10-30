# Stack Overflow Connector for cognee
    
This is a `dlt` source that reads questions and answers from Stack Overflow using the StackExchange API.

## Usage

```python
import cognee
from cognee_community_connector_stack_overflow import stack_overflow_questions

await cognee.remember(
    stack_overflow_questions(api_key="YOUR_KEY", tags=["python", "fastapi"]),
    dataset_name="stack_overflow",
    primary_key="id",
    write_disposition="merge",
    max_rows_per_table=0,
)
```
