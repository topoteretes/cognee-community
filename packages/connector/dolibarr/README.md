# Dolibarr ERP & CRM Community Connector for Cognee

An enterprise REST API connector integrating **Dolibarr Open Source ERP/CRM** with **Cognee**. Ingest third-party accounts, contacts, commercial quotes/proposals, and customer support tickets into Cognee's cognitive memory graph.

## Features

- **Third-Parties & Accounts**: Ingests client and supplier records, addresses, contact details, modification timestamps, and public notes.
- **Contacts & Directory**: Ingests organizational contacts with job positions, direct phone lines, and email addresses.
- **Commercial Proposals (Quotes)**: Ingests quotation references, financial sums (excl. and incl. tax), and proposal lifecycle states.
- **Customer Support Tickets**: Ingests support ticket subjects, categories, severity ratings, tracking codes, and initial message bodies.
- **Dolibarr 404 Quirk Handling**: Native parsing gracefully treats Dolibarr's 404 "No records found" responses as empty collections rather than crashing pipelines.
- **Privacy Controls**: Strict `include_private_notes=False` default keeps internal restricted notes out of vector databases unless explicitly authorized.

## Installation

```bash
pip install cognee-community-connector-dolibarr
```

## Quickstart

```python
import asyncio
from cognee_community_connector_dolibarr import DolibarrConnector

async def main():
    connector = DolibarrConnector(
        base_url="https://your-dolibarr-instance.org",
        api_key="your_dolapikey_secret",
        include_private_notes=False
    )

    # Ingest ERP/CRM records into Cognee
    documents = await connector.to_documents(
        include_thirdparties=True,
        include_contacts=True,
        include_proposals=True,
        include_tickets=True,
        limit_per_type=25
    )

    # await cognee.add(documents)
    # await cognee.cognify()

    await connector.close()

if __name__ == "__main__":
    asyncio.run(main())
```

## Running Tests

Execute the hermetic unit tests offline:

```bash
pytest packages/connector/dolibarr/tests/test_dolibarr.py -v
```
