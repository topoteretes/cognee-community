# Calendly Connector for Cognee

Data-source connector that syncs your Calendly events and attendee data into Cognee's AI memory platform.

## Installation

```bash
pip install "cognee-community-connector-calendly"
```

## Setup

1. Get a Calendly Personal Access Token:
   - Go to https://calendly.com/integrations/api_tokens
   - Generate a new token

2. Set the environment variable:
   ```bash
   export CALENDLY_API_KEY="your_token_here"
   ```

## Usage

```python
from cognee_community_connector_calendly import calendly_source

# Create the source
source = calendly_source()

# Use with Cognee
import cognee
await cognee.add(source)
await cognee.remember(source)
```

## Configuration

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `token` | str | `CALENDLY_API_KEY` env var | Personal Access Token |
| `user_uri` | str | Auto-detected | Calendly user URI |
| `organization_uri` | str | `CALENDLY_ORG_URI` env var | Organization URI |
| `min_start_time` | str | None | Filter: events after this ISO 8601 time |
| `max_start_time` | str | None | Filter: events before this ISO 8601 time |
| `status` | str | `"active"` | Filter: `active` or `canceled` |

## Data Extracted

- Event types and scheduled events
- Attendee details (name, email, timezone)
- Questions and answers
- Location and meeting links
- Cancellation status

## License

MIT
