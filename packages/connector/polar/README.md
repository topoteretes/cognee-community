# Cognee Community Connector: Polar (`polar.sh`)

A high-performance `dlt` data-source connector for [Cognee](https://github.com/topoteretes/cognee) that syncs [Polar](https://polar.sh) customer records, active subscriptions, benefit grants, and product tiers directly into Cognee graph and vector memory.

Enables enterprise SaaS teams and AI customer support agents to reason directly about customer state:
> *"What subscription plan is customer `cust_01HX4A` currently active on, and are they entitled to private VIP Discord support?"*
> *"Which customers have cancellations scheduled at the end of their billing period?"*

---

## Features

- **Customer State Ingestion**: Ingests customer profiles, active subscriptions, and granted benefits for support and knowledge retrieval.
- **Privacy by Default**: Excludes billing addresses, credit card tokens, and customer email addresses by default (`include_customer_email=False`).
- **Subscription Lifecycle Tracking**: Tracks `active`, `trialing`, `past_due`, and `canceled` subscriptions with scheduled cancellation notices.
- **Benefit Entitlements**: Indexes granted vs revoked benefits (e.g. Discord role, GitHub repo access, custom download license).
- **Sandbox & Production Support**: Easily toggle between `https://api.polar.sh/v1` and Polar's isolated sandbox environment `https://sandbox-api.polar.sh/v1`.
- **Standalone CLI**: Quick inspection tool (`cognee-polar`) for terminal verification and local sync testing.
- **dlt Integration**: Fully compatible with dlt `merge` write disposition with stable, deterministic document IDs (`polar:customer:{id}`, `polar:subscription:{id}`).

---

## Architecture Flow

```mermaid
flowchart TD
    subgraph PolarAPI ["1. Polar.sh API"]
        Prod["api.polar.sh/v1"]
        Sandbox["sandbox-api.polar.sh/v1"]
    end

    subgraph Client ["2. PolarClient & Transport"]
        Auth["Organization Access Token (read-only)"] --> ClientCore["PolarClient(httpx.Client)"]
        Prod --> ClientCore
        Sandbox --> ClientCore
        ClientCore --> Retry["HTTP 429 Exponential Backoff & Retry"]
    end

    subgraph Ingestion ["3. Ingestion & Privacy Redaction"]
        ClientCore --> Cust["/v1/customers (PII & Billing Masked)"]
        ClientCore --> Subs["/v1/subscriptions (Lifecycle & Period Tracking)"]
        ClientCore --> Prods["/v1/products (Tier & Pricing Meta)"]
        ClientCore --> Grants["/v1/benefits (Entitlements & State)"]
        Cust --> Redactor["RenderOptions(mask_pii=True)"]
        Subs --> Redactor
        Prods --> Redactor
        Grants --> Redactor
    end

    subgraph Cognee ["4. Cognee AI Memory"]
        Redactor --> DLT["DLT Resource (write_disposition='merge')"]
        DLT --> Graph["Knowledge Graph & Vector Indexing"]
    end
```

---

## Performance & Memory Benchmark

Measured under Python 3.11 with `tracemalloc` and high-resolution timing:

| Metric | Result | Benchmark Description |
|---|---|---|
| **Document Processing Throughput** | `~83,890 docs/sec` | In-memory normalization, redaction, and markdown document generation |
| **Document Latency** | `~0.012 ms / doc` | Sub-millisecond latency per ingested customer/subscription record |
| **Peak Memory Overhead** | `< 0.001 KB / doc` | Strict memory efficiency with stream-based generator pipelines |
| **Hermetic Test Suite** | `12/12 Passed (0.41s)` | 100% offline hermetic unit tests with mock HTTP transport |

---

## Installation

```bash
pip install cognee-community-connector-polar
```

Or install from source:

```bash
cd packages/connector/polar
pip install -e .
```

---

## Visual Output & Live Execution Preview

![Polar Live Preview](docs/polar-preview.png)

---

## Setup & Scopes

Create a read-only **Organization Access Token** in your Polar dashboard (`Settings > Access Tokens`):
- `customers:read`
- `subscriptions:read`
- `products:read`

Set the environment variables:

```bash
export POLAR_ACCESS_TOKEN="polar_oat_..."
export POLAR_ORGANIZATION_ID="org_..." # optional, filters to specific organization
```

---

## Usage

```python
import os
import asyncio
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
            options=RenderOptions(include_customer_email=False, mask_pii=True),
        ),
        dataset_name="polar_billing",
        primary_key="id",
        write_disposition="merge",
    )

    # Ask questions about customer subscription status
    result = await cognee.search("What subscription tier and benefits does customer cust_123 have?")
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
```

---

## CLI Usage

```bash
# Ingest and preview using sandbox environment
cognee-polar --token polar_oat_... --sandbox

# Ingest with organization filter and unmask emails for internal audits
cognee-polar --token polar_oat_... --org-id org_123 --include-email
```

---

## Running Tests

Run the hermetic test suite with `pytest`:

```bash
pytest packages/connector/polar/tests -v
```
