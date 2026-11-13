# Mailgun Email Telemetry & Template Connector for Cognee

A production-grade [Cognee](https://github.com/topoteretes/cognee) community data-source connector for **[Mailgun by Sinch](https://www.mailgun.com/)**, the industry-standard cloud email service delivering over 10 billion messages monthly for Shopify, GitHub, Lyft, and Wikipedia.

This connector ingests sending domains, DNS health (SPF, DKIM, MX), delivery analytics (accepted, delivered, failed, complained), email templates, version snapshots, and bounce failure diagnostics into structured Markdown documents optimized for Cognee's `cognify()` knowledge graph pipeline and semantic vector retrieval.

---

## Key Capabilities

- **Sending Domain & DNS Verification**: Extracts sending domains, spam action rules, and DKIM/SPF DNS verification status.
- **Deliverability & Engagement Analytics**: Ingests accepted, delivered, temporary/permanent fail rates, and user complaint counts.
- **Email Template & Version Tracking**: Ingests transactional and marketing email templates, active version tags, and HTML/text previews.
- **Bounce & Failure Diagnostics**: Captures recipient addresses, Mailgun bounce codes, and SMTP diagnostic error strings.
- **US and EU Region Routing**: Configurable `is_eu_region` supporting GDPR-compliant European Mailgun endpoints (`api.eu.mailgun.net`).
- **Document-Mode Routing**: Sets `DOCUMENT_SOURCE_ATTR = "mailgun"` ensuring documents route through Cognee's downstream cognitive extraction.
- **Orphan Cleanup (`write_disposition="replace"`)**: Automatically syncs the latest state and cleans up deleted domains or outdated templates in Cognee storage.
- **Deterministic Document IDs**: Uses stable `uuid5` hashing so subsequent syncs idempotently update existing memory nodes without duplicate creation.
- **Security & Key Masking**: Automatically redacts API keys (`[REDACTED_MAILGUN_KEY]`) from logs, error messages, and exception traces.

---

## Installation

```bash
pip install cognee cognee-community-connector-mailgun
```

Or using `uv`:

```bash
uv add cognee cognee-community-connector-mailgun
```

---

## Configuration Reference

The connector is configured using `MailgunConfig`:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `api_key` | `str` | *required* | Mailgun API key (e.g. `key-xxx` or Mailgun Send API key) |
| `domains` | `list[str] \| None` | `None` | Specific sending domains to ingest (or `None` for all configured) |
| `is_eu_region` | `bool` | `False` | Whether to route requests to the EU Mailgun endpoint (`api.eu.mailgun.net`) |
| `include_stats` | `bool` | `True` | Whether to ingest delivery, bounce, and engagement statistics |
| `include_templates` | `bool` | `True` | Whether to ingest email templates and active versions |
| `include_bounces` | `bool` | `True` | Whether to ingest recent bounce events and failure diagnostics |
| `limit` | `int` | `50` | Pagination limit for bounce records |
| `request_timeout` | `float` | `30.0` | HTTP request timeout in seconds |

---

## Quickstart

```python
import asyncio
import os
import cognee
from cognee_community_connector_mailgun import (
    MailgunConfig,
    mailgun_connector,
)


async def main():
    config = MailgunConfig(
        api_key=os.getenv("MAILGUN_API_KEY", "key-example"),
        is_eu_region=False,
        include_stats=True,
        include_templates=True,
        include_bounces=True,
    )

    # 1. Fetch email telemetry and templates
    documents = await mailgun_connector(config)
    print(f"Retrieved {len(documents)} email infrastructure documents.")

    # 2. Add to Cognee memory
    await cognee.add(documents, dataset_name="email_infrastructure")

    # 3. Cognify into knowledge graph
    await cognee.cognify(dataset_name="email_infrastructure")

    # 4. Query email deliverability and templates
    query = "What are the common bounce failure reasons on mg.example.com?"
    results = await cognee.search(query, dataset_name="email_infrastructure")
    for r in results:
        print(f"Memory match: {r}")


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Running Offline Tests

The connector includes comprehensive offline unit tests that run with synthetic mocked HTTP transports (no live Mailgun account or network connection required):

```bash
uv run --project packages/connector/mailgun --with pytest --with pytest-asyncio pytest packages/connector/mailgun/tests -p no:typeguard -v
```

---

## License

Apache-2.0
