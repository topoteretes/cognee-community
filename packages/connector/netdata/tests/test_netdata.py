"""Unit tests for the Netdata infrastructure monitoring data-source connector.

All tests run completely offline without live server access or network requests,
using synthetic Netdata API fixtures and mocked HTTP transports.
"""

from __future__ import annotations

import httpx
import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

from cognee_community_connector_netdata.netdata import (
    NETDATA_SOURCE_NAME,
    NETDATA_TABLE_NAME,
    NetdataAPIError,
    NetdataAuthError,
    NetdataClient,
    NetdataConfig,
    NetdataRateLimitError,
    _build_alarms_markdown,
    _build_charts_markdown,
    _build_node_markdown,
    _redact_token,
    netdata_connector,
    netdata_source,
)

# ---------------------------------------------------------------------------
# Synthetic Test Fixtures
# ---------------------------------------------------------------------------

SAMPLE_INFO = {
    "version": "v1.45.0",
    "uid": "node-prod-101",
    "mirrored_hosts": ["prod-api-worker-01"],
    "alarms": {
        "normal": 45,
        "warning": 2,
        "critical": 1,
    },
    "os_name": "Ubuntu",
    "os_version": "22.04 LTS",
    "kernel_name": "Linux",
    "kernel_version": "5.15.0-89-generic",
    "architecture": "x86_64",
    "cores_total": 8,
    "total_ram": 17179869184,  # 16 GB
}

SAMPLE_ALARMS = {
    "hostname": "prod-api-worker-01",
    "alarms": {
        "system.cpu.10min_cpu_usage": {
            "id": 1001,
            "name": "10min_cpu_usage",
            "chart": "system.cpu",
            "status": "WARNING",
            "value": 87.5,
            "units": "%",
            "info": "Average CPU utilization over 10 minutes",
            "recipient": "sysadmin",
        },
        "disk_space._.disk_fill_rate": {
            "id": 1002,
            "name": "disk_fill_rate",
            "chart": "disk_space._",
            "status": "CRITICAL",
            "value": 96.2,
            "units": "%",
            "info": "Disk volume usage approaching full capacity",
            "recipient": "sre-oncall",
        },
    },
}

SAMPLE_CHARTS = {
    "hostname": "prod-api-worker-01",
    "charts": {
        "system.cpu": {
            "name": "system.cpu",
            "type": "stacked",
            "family": "cpu",
            "context": "system.cpu",
            "units": "percentage",
            "title": "Total CPU utilization",
        },
        "system.ram": {
            "name": "system.ram",
            "type": "stacked",
            "family": "ram",
            "context": "system.ram",
            "units": "MB",
            "title": "System RAM",
        },
    },
}


class MockTransport(httpx.BaseTransport):
    """Mock HTTP transport returning synthetic Netdata API responses."""

    def __init__(self, status_code: int = 200, error_text: str | None = None) -> None:
        self.status_code = status_code
        self.error_text = error_text
        self.call_count = 0
        self.last_request = None

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.call_count += 1
        self.last_request = request

        if self.status_code != 200:
            text = self.error_text or "Simulated Netdata error"
            return httpx.Response(status_code=self.status_code, text=text)

        path = request.url.path
        if "/api/v1/info" in path:
            return httpx.Response(200, json=SAMPLE_INFO)
        if "/api/v1/alarms" in path:
            return httpx.Response(200, json=SAMPLE_ALARMS)
        if "/api/v1/charts" in path:
            return httpx.Response(200, json=SAMPLE_CHARTS)

        return httpx.Response(200, json={"status": "ok"})


# ---------------------------------------------------------------------------
# Helper & Redaction Tests
# ---------------------------------------------------------------------------


def test_redact_token() -> None:
    token = "netdata_secret_token_123456"
    msg = f"Failed to authenticate with bearer {token} on Netdata agent"
    redacted = _redact_token(msg, token)

    assert token not in redacted
    assert "[REDACTED_NETDATA_TOKEN]" in redacted


def test_redact_token_short_or_none() -> None:
    assert _redact_token("error msg", None) == "error msg"
    assert _redact_token("error msg", "12") == "error msg"


# ---------------------------------------------------------------------------
# NetdataClient Transport & Auth Tests
# ---------------------------------------------------------------------------


def test_client_custom_base_url() -> None:
    client = NetdataClient(
        base_url="http://node-01.internal.netdata:19999/",
        api_token="test-token",
    )
    assert client.base_url == "http://node-01.internal.netdata:19999"


def test_client_fetches_info() -> None:
    transport = MockTransport()
    client = NetdataClient(client=httpx.Client(transport=transport))

    info = client.get_system_info()
    assert info["uid"] == "node-prod-101"
    assert info["os_name"] == "Ubuntu"
    assert info["cores_total"] == 8


def test_client_fetches_alarms_and_charts() -> None:
    transport = MockTransport()
    client = NetdataClient(client=httpx.Client(transport=transport))

    alarms = client.get_alarms()
    assert "alarms" in alarms
    assert len(alarms["alarms"]) == 2

    charts = client.get_charts()
    assert "charts" in charts
    assert "system.cpu" in charts["charts"]


def test_client_handles_auth_error_401() -> None:
    token = "invalid-netdata-token"
    transport = MockTransport(
        status_code=401,
        error_text=f"Unauthorized access for token: {token}",
    )
    client = NetdataClient(api_token=token, client=httpx.Client(transport=transport))

    with pytest.raises(NetdataAuthError) as exc_info:
        client.get_system_info()

    assert token not in str(exc_info.value)
    assert "[REDACTED_NETDATA_TOKEN]" in str(exc_info.value)


def test_client_handles_rate_limit_429() -> None:
    transport = MockTransport(status_code=429)
    client = NetdataClient(client=httpx.Client(transport=transport))

    with pytest.raises(NetdataRateLimitError):
        client.get_system_info()


def test_client_handles_server_error_500() -> None:
    transport = MockTransport(status_code=500)
    client = NetdataClient(client=httpx.Client(transport=transport))

    with pytest.raises(NetdataAPIError):
        client.get_system_info()


# ---------------------------------------------------------------------------
# Markdown Formatting Tests
# ---------------------------------------------------------------------------


def test_build_node_markdown() -> None:
    info = SAMPLE_INFO
    md = _build_node_markdown(info, "http://localhost:19999")

    assert "# Netdata Node System & Infrastructure Overview" in md
    assert "node-prod-101" in md
    assert "Ubuntu 22.04 LTS" in md
    assert "Total CPU Cores**: 8" in md
    assert "16.00 GB" in md
    assert "Critical Alarms**: 1" in md


def test_build_alarms_markdown() -> None:
    alarms = SAMPLE_ALARMS
    md = _build_alarms_markdown("prod-api-worker-01", alarms)

    assert "# Netdata Active Health Alarms: prod-api-worker-01" in md
    assert "10min_cpu_usage" in md
    assert "WARNING" in md
    assert "87.5 %" in md
    assert "disk_fill_rate" in md
    assert "CRITICAL" in md
    assert "96.2 %" in md


def test_build_charts_markdown() -> None:
    charts = SAMPLE_CHARTS
    md = _build_charts_markdown("prod-api-worker-01", charts)

    assert "# Netdata Monitored Subsystems & Chart Catalog: prod-api-worker-01" in md
    assert "system.cpu" in md
    assert "Total CPU utilization" in md
    assert "system.ram" in md


# ---------------------------------------------------------------------------
# DLT Source Invariant & Integration Tests
# ---------------------------------------------------------------------------


def test_netdata_source_document_mode_invariant() -> None:
    transport = MockTransport()
    fake_client = NetdataClient(client=httpx.Client(transport=transport))

    config = NetdataConfig()
    source = netdata_source(config=config, client=fake_client)

    # Invariant: Must declare DOCUMENT_SOURCE_ATTR = "netdata"
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == NETDATA_SOURCE_NAME


def test_netdata_source_yields_expected_reports() -> None:
    transport = MockTransport()
    fake_client = NetdataClient(client=httpx.Client(transport=transport))

    config = NetdataConfig(include_alarms=True, include_charts=True)
    source = netdata_source(config=config, client=fake_client)

    resources = list(source.selected_resources.values())
    assert len(resources) == 1
    resource = resources[0]
    assert resource.name == NETDATA_TABLE_NAME
    assert resource.write_disposition == "replace"

    rows = list(resource())
    # 1 node info + 1 alarms + 1 charts = 3 documents
    assert len(rows) == 3

    report_types = [r["metadata"]["report_type"] for r in rows]
    assert "node_overview" in report_types
    assert "active_alarms" in report_types
    assert "metrics_catalog" in report_types

    for r in rows:
        assert r["metadata"]["source"] == NETDATA_SOURCE_NAME
        assert len(r["id"]) > 0
        assert r["text"].startswith("# ")


def test_netdata_source_deterministic_id_stability() -> None:
    transport = MockTransport()
    fake_client = NetdataClient(client=httpx.Client(transport=transport))

    config = NetdataConfig()

    source_1 = netdata_source(config=config, client=fake_client)
    rows_1 = list(next(iter(source_1.selected_resources.values()))())

    source_2 = netdata_source(config=config, client=fake_client)
    rows_2 = list(next(iter(source_2.selected_resources.values()))())

    # Deterministic IDs ensure repeat runs update existing nodes without duplicates
    assert [r["id"] for r in rows_1] == [r["id"] for r in rows_2]


def test_netdata_source_status_filter() -> None:
    transport = MockTransport()
    fake_client = NetdataClient(client=httpx.Client(transport=transport))

    config = NetdataConfig(
        include_alarms=True,
        include_charts=False,
        alarm_status_filter=["CRITICAL"],
    )
    source = netdata_source(config=config, client=fake_client)

    resource = next(iter(source.selected_resources.values()))
    rows = list(resource())

    assert len(rows) == 2  # 1 node info + 1 alarms report
    alarms_row = next(r for r in rows if r["metadata"]["report_type"] == "active_alarms")
    assert "CRITICAL" in alarms_row["text"]
    assert "10min_cpu_usage" not in alarms_row["text"]  # WARNING was filtered out


@pytest.mark.asyncio
async def test_netdata_connector_async_wrapper() -> None:
    transport = MockTransport()
    fake_client = NetdataClient(client=httpx.Client(transport=transport))

    config = NetdataConfig(
        include_alarms=True,
        include_charts=True,
    )

    documents = await netdata_connector(config, client=fake_client)
    assert len(documents) == 3
    for doc in documents:
        assert "id" in doc
        assert "text" in doc
        assert doc["metadata"]["source"] == "netdata"
