# Intercom Connector for Cognee

This connector pulls your contacts, conversations, and other data from Intercom and ingests them into Cognee.

## Setup
1. Create an Intercom Access Token in your Developer Workspace.
2. Set the `INTERCOM_ACCESS_TOKEN` environment variable.

## Usage
```python
import os
import dlt
from cognee_community_connector_intercom.intercom import intercom_source

os.environ["INTERCOM_ACCESS_TOKEN"] = "your_access_token_here"

# Extracts contacts and conversations by default
source = intercom_source()

pipeline = dlt.pipeline(
    pipeline_name="intercom_pipeline",
    destination="duckdb",
    dataset_name="intercom_data"
)

pipeline.run(source)
```
