# RevenueCat Connector for Cognee

An in-app subscriptions, mobile billing, and customer entitlement connector to synchronize subscribers, product renewals, churn events, and trial metrics from **RevenueCat** directly into **Cognee** knowledge graphs.

## Features
- **In-App Entitlement Mapping:** Ingests active subscriber entitlements across iOS App Store, Google Play Store, and Stripe.
- **Subscription Lifecycle Signals:** Tracks purchase dates, expiration timestamps, period frequencies, and cancellation occurrences.
- **Natural Language Product Analytics:** Enables AI agents to answer queries like *"Which users on enterprise trials churned this week?"* or *"What plans are driving mobile subscription renewals?"*.
- **Comprehensive Unit Testing:** Hermetic offline test suite with zero live credential requirement.

## Installation

```bash
pip install cognee-community-connector-revenuecat
```

## Quick Start

```python
from cognee_community_connector_revenuecat import RevenueCatConnector

connector = RevenueCatConnector(api_key="YOUR_REVENUECAT_SECRET_KEY")
documents = connector.fetch_documents_for_users(["user_123", "user_456"])

for doc in documents:
    print(f"Ingested {doc['metadata']['type']}: {doc['metadata']['app_user_id']}")
    print(doc["text"][:150])
```
