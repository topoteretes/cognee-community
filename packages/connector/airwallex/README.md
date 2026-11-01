# Airwallex Connector for Cognee

An enterprise treasury, multi-currency balance, and global payments connector to synchronize international payouts, transfer reasons, and cash balances from **Airwallex** directly into **Cognee** knowledge graphs.

## Features
- **Global Payouts & Transfers:** Tracks beneficiary details, banks, currencies (USD, EUR, GBP, AUD, SGD, HKD), status, and failure causes.
- **Multi-Currency Treasury Balances:** Ingests real-time available, reserved, and pending balances across financial ledgers.
- **Natural Language Treasury Inquiries:** Ask agents questions like *"Did our supplier payout to Cloudflare settle successfully?"* or *"What is our available cash across USD and EUR balances?"*.
- **Comprehensive Unit Testing:** Hermetic offline test suite with zero live credential requirement.

## Installation

```bash
pip install cognee-community-connector-airwallex
```

## Quick Start

```python
from cognee_community_connector_airwallex import AirwallexConnector

connector = AirwallexConnector(api_token="YOUR_BEARER_TOKEN")
documents = connector.fetch_documents(page_size=50)

for doc in documents:
    print(f"Ingested {doc['metadata']['type']}: {doc['metadata']}")
    print(doc["text"][:150])
```
