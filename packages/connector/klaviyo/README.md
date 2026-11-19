# Klaviyo Connector for Cognee

This connector pulls your email marketing campaigns, lists, segments, and profiles from Klaviyo and ingests them into Cognee.

## Setup
1. Create a Klaviyo Private API Key in your Klaviyo account settings.
2. Set the `KLAVIYO_API_KEY` environment variable.

## Usage
```python
import os
import dlt
from cognee_community_connector_klaviyo.klaviyo import klaviyo_source

os.environ["KLAVIYO_API_KEY"] = "your_private_api_key_here"

# Extracts campaigns by default
source = klaviyo_source()

# To extract other entities like lists or segments
# source = klaviyo_source(endpoints=["campaigns", "lists", "segments"])

pipeline = dlt.pipeline(
    pipeline_name="klaviyo_pipeline",
    destination="duckdb",
    dataset_name="klaviyo_data"
)

pipeline.run(source)
```
