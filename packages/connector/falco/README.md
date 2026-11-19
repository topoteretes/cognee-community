# Falco Runtime Security & Threat Detection Connector for Cognee

The official `falco` data-source connector for [Cognee](https://github.com/topoteretes/cognee), syncing cloud-native runtime security alerts, container syscall detections, process executions, and MITRE ATT&CK techniques directly into AI agent memory and relationship graphs.

---

## Overview

Kubernetes clusters and container environments face active runtime threats—such as shells spawned inside containers, attempts to read sensitive files like `/etc/shadow`, unexpected outbound connections, and namespace escapes.

[Falco](https://falco.org/) (the CNCF graduated runtime security engine) catches these behaviors live in the Linux kernel. This connector streams Falco alerts (from `falco.yaml` log files, directories, or Falcosidekick HTTP webhooks) directly into Cognee's document ingestion pipeline (`cognify()`).

Security agents can then answer questions like:
- *"Which pods in the payments namespace triggered critical Falco alerts this week?"*
- *"What commands were executed by root inside containers before the pod was terminated?"*
- *"Connect this Falco alert to the Kubernetes workload and MITRE ATT&CK mitigation steps in our knowledge graph."*

### Key Capabilities

- **Multi-Source Ingestion**: Ingests Falco JSON Lines files (`output_file` from `falco.yaml`), JSON arrays, log directories, or Falcosidekick HTTP webhook payloads.
- **Priority Severity Thresholds**: Filters alerts based on standard Falco priority levels (`Emergency` > `Alert` > `Critical` > `Error` > `Warning` > `Notice` > `Informational` > `Debug`).
- **Targeted Scoping**: Filters by Kubernetes namespaces (e.g. `["production", "payments"]`), specific Falco rule names, or tags.
- **Command-Line Credential Redaction**: Automatically scans and masks command-line secrets (`proc.cmdline`, `proc.args`), including bearer tokens, passwords, AWS access keys (`AKIA...`), secret access keys, JWTs, and API keys before LLM indexing.
- **Workload Aggregation & Content-Hash Stability**: Aggregates repeated alerts into workload-level documents `(cluster/namespace/workload/rule)`. Volatile fields like nanosecond timestamps and ephemeral container IDs are omitted from the main text body to maintain content-hash stability and avoid redundant `cognify()` re-computations.
- **Document-Mode Routing**: Declares `DOCUMENT_SOURCE_ATTR = "falco"` for automated knowledge graph extraction and attaches `cognee_node_set` (cluster, namespace, runtime security) for pinpoint scoped recall.
- **Reconciliation & Orphan Cleanup**: Employs `write_disposition="replace"` so resolved alerts are automatically cleaned up by Cognee's `orphan_cleanup`.

---

## Installation

```bash
pip install "cognee-community-connector-falco"
```

Or from the `cognee-community` monorepo:

```bash
cd packages/connector/falco
pip install -e .
```

---

## Quickstart

### 1. Ingest Falco Alerts into Cognee

```python
import asyncio
import cognee
from cognee_community_connector_falco import falco_source


async def main():
    # 1. Initialize connector with Falco logs or webhook payloads
    source = falco_source(
        input_files="/var/log/falco_alerts.json",
        min_priority="Warning",
        namespaces=["production", "payments"],
        cluster_name="k8s-prod-us-east-1",
        group_by_workload=True,
    )

    # 2. Ingest into Cognee staging
    await cognee.add(source)

    # 3. Cognify: extract entities, relationships, and vector embeddings
    await cognee.cognify()

    # 4. Query agent memory with security context
    results = await cognee.search(
        "Which workloads triggered critical Falco alerts and spawned interactive shells?"
    )
    for res in results:
        print(res)


if __name__ == "__main__":
    asyncio.run(main())
```

---

### 2. Ingesting Falcosidekick Webhook Payloads (In-Memory)

```python
from cognee_community_connector_falco import falco_source

webhook_payload = {
    "output": "15:04:12: Critical A shell was spawned in container (k8s.ns=production k8s.pod=payment-xyz container=payment-svc)",
    "priority": "Critical",
    "rule": "Terminal shell in container",
    "output_fields": {
        "k8s.ns.name": "production",
        "k8s.pod.name": "payment-xyz",
        "proc.cmdline": "bash -i --password secret123",
        "proc.name": "bash",
    },
    "tags": ["container", "mitre_execution", "mitre_t1059"],
}

source = falco_source(
    payloads=[webhook_payload],
    min_priority="Warning",
    cluster_name="prod-cluster",
)
```

---

## Document Schema & Metadata

Each alert or aggregated workload group is formatted as a structured Markdown document:

| Field | Type | Description |
|---|---|---|
| `id` | `str` | Workload primary key `{cluster}/{namespace}/{workload}/{rule}` or alert UUID |
| `title` | `str` | Standardized header `[Falco Alert] <Rule> in <Namespace> (<Count> events)` |
| `url` | `str` | URI pointer `falco://<id>` |
| `cluster` | `str` | Kubernetes cluster identifier |
| `namespace` | `str` | Kubernetes namespace |
| `rule` | `str` | Falco detection rule name |
| `priority` | `str` | Severity level (`Critical`, `Warning`, etc.) |
| `event_count` | `int` | Total detections aggregated within the window |
| `cognee_node_set` | `list[str]` | Scoped tags (`cluster:<name>`, `namespace:<ns>`, `falco`, `runtime_security`) |
| `content` | `str` | High-signal Markdown body with sanitized commands, MITRE tags, and remediation |

---

## Running Tests

Run the hermetic test suite:

```bash
pytest packages/connector/falco/tests/ -v
```
