# Calendly Connector for Cognee

This connector pulls your scheduled events and event types from Calendly and ingests them into Cognee.

## Setup
1. Create a Personal Access Token in your Calendly integrations page.
2. Set the `CALENDLY_PERSONAL_ACCESS_TOKEN` environment variable.

## Usage
```python
import os
import dlt
from cognee_community_connector_calendly.calendly import calendly_source

os.environ["CALENDLY_PERSONAL_ACCESS_TOKEN"] = "your_personal_access_token_here"

# Extracts event_types and scheduled_events by default
source = calendly_source()

pipeline = dlt.pipeline(
    pipeline_name="calendly_pipeline",
    destination="duckdb",
    dataset_name="calendly_data"
)

pipeline.run(source)
```
