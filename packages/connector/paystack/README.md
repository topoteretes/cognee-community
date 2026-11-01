# Paystack Connector for Cognee

An enterprise fintech and merchant data connector to synchronize online transactions, refunds, and customer payment histories from **Paystack** directly into **Cognee** knowledge graphs.

## Features
- **Transaction Ingestion:** Extracts transaction references, customer names, emails, channels, amounts (NGN, GHS, ZAR, KES, USD), and gateway statuses.
- **Refund Tracking:** Monitors processed refunds and reasons linked directly to transaction nodes.
- **Natural Language Financial Intelligence:** Enables AI agents in Cognee to answer questions like *"Did customer X have a failed card charge yesterday?"* or *"Summarize today's successful Paystack payouts"*.
- **Comprehensive Unit Testing:** Hermetic offline test suite with zero live credential requirement.

## Installation

```bash
pip install cognee-community-connector-paystack
```

## Quick Start

```python
from cognee_community_connector_paystack import PaystackConnector

connector = PaystackConnector(secret_key="sk_live_...")
documents = connector.fetch_documents(page_size=50)

for doc in documents:
    print(f"Ingested {doc['metadata']['type']}: {doc['metadata']['reference']}")
    print(doc["text"][:150])
```
