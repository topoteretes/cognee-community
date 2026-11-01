# Cognee Community Connector: Polar (`polar.sh`)

Data-source connector for [Polar](https://polar.sh) — sync subscriptions, customer states, benefit grants, and product tiers into Cognee AI memory as searchable documents.

---

## Capabilities

- **Customer State Ingestion**: Ingests customer profiles, active subscriptions, and granted benefits for support and knowledge retrieval.
- **Privacy by Default**: Excludes billing addresses, credit card tokens, and customer email addresses by default (`include_customer_email=False`).
- **Subscription Lifecycle Tracking**: Tracks active, trialing, past due, and canceled subscriptions with scheduled cancellation notices.
- **Sandbox & Production Support**: Easily toggle between `https://api.polar.sh/v1` and Polar's isolated sandbox environment `https://sandbox-api.polar.sh/v1`.
- **dlt Integration**: Compatible with dlt `merge` write disposition with stable, deterministic document IDs.

---

## Installation

```bash
pip install cognee-community-connector-polar
```

Or with `uv`:

```bash
uv pip install cognee-community-connector-polar
```

---

## Setup & Scopes

Create a read-only **Organization Access Token** in your Polar dashboard:
- `customers:read`
- `subscriptions:read`
- `products:read`

Set the environment variable:

```bash
export POLAR_ACCESS_TOKEN="polar_oat_..."
export POLAR_ORGANIZATION_ID="org_..." # optional, filters to specific organization
```

---

## Usage

```python
import os
import cognee
from cognee_community_connector_polar.polar import polar_source, RenderOptions

async def main():
    token = os.environ["POLAR_ACCESS_TOKEN"]
    org_id = os.environ.get("POLAR_ORGANIZATION_ID")

    # Ingest Polar data into Cognee memory
    await cognee.remember(
        polar_source(
            access_token=token,
            organization_id=org_id,
            options=RenderOptions(include_customer_email=False),
        ),
        dataset_name="polar_billing",
        primary_key="id",
        write_disposition="merge",
    )

    # Ask questions about customer subscription status
    result = await cognee.search(
        "What subscription tier and benefits does customer cust_123 have?"
    )
    print(result)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
```

---

## Running Tests

Run the hermetic test suite with `pytest`:

```bash
pytest packages/connector/polar/tests -v
```
