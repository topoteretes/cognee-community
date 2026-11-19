# Netdata Infrastructure Monitoring Connector for Cognee

A production-grade [Cognee](https://github.com/topoteretes/cognee) community data-source connector for **[Netdata](https://www.netdata.cloud/)**, the high-performance distributed real-time infrastructure and server monitoring system with over 70,000 GitHub stars on `netdata/netdata`.

This connector ingests server node hardware topology (CPU cores, RAM capacity, kernel versions, OS distribution), active health alarms (CRITICAL, WARNING), and monitored subsystem chart catalogs into structured Markdown documents optimized for Cognee's `cognify()` knowledge graph pipeline and semantic vector retrieval.

---

## Key Capabilities

- **Node Hardware & OS Topology**: Ingests hostnames, operating system releases, kernel architectures, vCPU counts, and total RAM capacity.
- **Active Health Alarms & Diagnostics**: Captures triggered alarm states (CRITICAL, WARNING), trigger values, units, calculation formulas, and alert recipients.
- **Subsystem Metric Catalog**: Ingests monitored system charts across CPU, RAM, disk I/O, network interfaces, and container workloads.
- **Local Agent & Cloud Compatibility**: Works with standalone local Netdata agents (`http://localhost:19999`) or authenticated Netdata Cloud endpoints.
- **Document-Mode Routing**: Sets `DOCUMENT_SOURCE_ATTR = "netdata"` ensuring documents route through Cognee's downstream cognitive extraction.
- **Orphan Cleanup (`write_disposition="replace"`)**: Automatically removes resolved alarms or decommissioned nodes from Cognee storage.
- **Deterministic Document IDs**: Uses stable `uuid5` hashing so subsequent syncs idempotently update existing memory nodes without duplicate creation.
- **Security & Token Masking**: Automatically redacts API Bearer tokens (`[REDACTED_NETDATA_TOKEN]`) from logs, error messages, and exception traces.

---

## Installation

```bash
pip install cognee cognee-community-connector-netdata
```

Or using `uv`:

```bash
uv add cognee cognee-community-connector-netdata
```

---

## Configuration Reference

The connector is configured using `NetdataConfig`:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `base_url` | `str` | `http://localhost:19999` | Netdata agent or Cloud API base endpoint |
| `api_token` | `str \| None` | `None` | Optional Bearer auth token for protected endpoints |
| `include_alarms` | `bool` | `True` | Whether to extract active health alarms and failure diagnostics |
| `include_charts` | `bool` | `True` | Whether to extract monitored chart catalogs and metric definitions |
| `alarm_status_filter` | `list[str] \| None` | `None` | Filter alarms by status (e.g. `["CRITICAL", "WARNING"]`) |
| `request_timeout` | `float` | `30.0` | HTTP request timeout in seconds |

---

## Quickstart

```python
import asyncio
import os
import cognee
from cognee_community_connector_netdata import (
    NetdataConfig,
    netdata_connector,
)


async def main():
    config = NetdataConfig(
        base_url=os.getenv("NETDATA_BASE_URL", "http://localhost:19999"),
        api_token=os.getenv("NETDATA_API_TOKEN", None),
        include_alarms=True,
        include_charts=True,
        alarm_status_filter=["CRITICAL", "WARNING"],
    )

    # 1. Fetch infrastructure telemetry
    documents = await netdata_connector(config)
    print(f"Retrieved {len(documents)} infrastructure telemetry documents.")

    # 2. Add to Cognee memory
    await cognee.add(documents, dataset_name="infrastructure_telemetry")

    # 3. Cognify into knowledge graph
    await cognee.cognify(dataset_name="infrastructure_telemetry")

    # 4. Query active alerts and host topology
    query = "What critical alarms are currently active on the host?"
    results = await cognee.search(query, dataset_name="infrastructure_telemetry")
    for r in results:
        print(f"Memory match: {r}")


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Running Offline Tests

The connector includes comprehensive offline unit tests that run with synthetic mocked HTTP transports (no live Netdata agent or network connection required):

```bash
uv run --project packages/connector/netdata --with pytest --with pytest-asyncio pytest packages/connector/netdata/tests -p no:typeguard -v
```

---

## License

Apache-2.0
