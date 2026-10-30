# Polar Connector for Cognee

A `dlt`-based data source connector for [Cognee](https://github.com/topoteretes/cognee) that ingests [Polar.sh](https://polar.sh) billing and customer data directly into your graph memory knowledge base.

## Features

- **Customers**: Ingests customer profiles, emails, names, and organizations.
- **Subscriptions**: Indexes active and past subscription tiers, lifecycle statuses, and renewal windows.
- **Products**: Indexes available SaaS tiers, benefits, and descriptions.
- **Incremental & Merge-friendly**: Supports `merge` write disposition with deterministic primary keys.

## Quickstart

```python
import cognee
from cognee_community_connector_polar import polar_source

# Remember Polar data into Cognee graph memory
await cognee.remember(
    polar_source(
        access_token="polar_at_...",
        organization_id="org_...",
    ),
    dataset_name="polar_billing",
    primary_key="id",
    write_disposition="merge",
)
```

## Environment Variables

- `POLAR_ACCESS_TOKEN`: Your Polar.sh personal access token or organization token.
- `POLAR_ORGANIZATION_ID`: (Optional) Filter resources to a specific organization.
