# PagerDuty Connector for Cognee

A community data-source connector for **[Cognee](https://github.com/topoteretes/cognee)** that ingests on-call incident alerts, responder timelines, and operational incident reports from **[PagerDuty](https://www.pagerduty.com)** directly into Cognee's AI-agent memory graph.

---

## ⚡ What it Ingests

- **Incidents:** Incident title, status (`triggered`, `acknowledged`, `resolved`), urgency level (`high`, `low`), created timestamp, and duration.
- **Affected Services:** Technical service name triggering the incident (e.g. `Payment Gateway`, `Kubernetes Ingress`).
- **Responder Assignments:** Incident commanders and on-call responders assigned to triage the issue.
- **Triggered Alerts:** Detailed raw alert summaries and monitoring system telemetry associated with the incident.

---

## 🚀 Quickstart

### 1. Installation

```bash
pip install cognee-community-connector-pagerduty
```

### 2. Environment Variables

Set your PagerDuty REST API token (v2):

```bash
export PAGERDUTY_API_TOKEN="your_pagerduty_api_token"
```

### 3. Usage with Cognee

```python
import asyncio
import cognee
from cognee_community_connector_pagerduty import pagerduty_source

async def main():
    # Ingest active and resolved PagerDuty incidents
    source = pagerduty_source()
    await cognee.add(source)
    await cognee.cognify()

    # Search operational memory
    results = await cognee.search("Who responded to the high-urgency checkout incident?")
    print(results)

if __name__ == "__main__":
    asyncio.run(main())
```

---

## ⚙️ Configuration

| Parameter | Type | Default | Description |
|---|---|---|---|
| `api_token` | `str` | `None` | PagerDuty API token. Falls back to `PAGERDUTY_API_TOKEN`. |
| `statuses` | `list[str]` | `None` | Filter by incident status (`triggered`, `acknowledged`, `resolved`). |
| `urgencies` | `list[str]` | `None` | Filter by urgency (`high`, `low`). |
| `ingest_alerts` | `bool` | `True` | Whether to fetch and synthesize underlying per-incident alerts. |

---

## 🧪 Testing

The connector contains 100% offline unit tests with zero live network dependencies:

```bash
pytest tests/ -v
```

---

## 📄 License

Licensed under the [Apache 2.0 License](LICENSE).
