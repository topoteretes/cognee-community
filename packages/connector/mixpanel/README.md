# cognee-community-connector-mixpanel

Ingest a Mixpanel project's event schemas, saved cohorts, and saved reports as normal cognee documents. Optional bounded event export adds recent activity without unbounded historical ingestion.

## Install

```bash
uv pip install cognee-community-connector-mixpanel
```

## Configure

Create a Mixpanel service account and set:

```bash
export MIXPANEL_SERVICE_ACCOUNT_USERNAME="..."
export MIXPANEL_SERVICE_ACCOUNT_PASSWORD="..."
export MIXPANEL_PROJECT_ID="..."
```

Set `MIXPANEL_API_BASE_URL` for an EU/India residency endpoint or a compatible proxy. The connector uses a full snapshot for workspace artifacts, so removal from Mixpanel is reflected on the next sync.

## Usage

```python
import cognee
from cognee_community_connector_mixpanel import mixpanel_source

await cognee.remember(
    mixpanel_source(include_events=True),
    dataset_name="mixpanel",
)
```

Events are disabled by default. When enabled, the default window is the previous day through today; pass `event_start=` and `event_end=` for an explicit range.

## Test

```bash
uv run pytest tests/
```
