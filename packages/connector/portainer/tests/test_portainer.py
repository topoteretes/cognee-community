"""Unit tests for the Portainer data-source connector.

All tests run completely offline without live server access or network requests,
using synthetic monitoring fixtures and mocked HTTP transports.
"""

from __future__ import annotations

import httpx
import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

from cognee_community_connector_portainer.portainer import (
    PORTAINER_SOURCE_NAME,
    PORTAINER_TABLE_NAME,
    PortainerAuthError,
    PortainerClient,
    PortainerConfig,
    PortainerRateLimitError,
    _build_containers_markdown,
    _build_environments_markdown,
    _build_stacks_markdown,
    _endpoint_type_name,
    _redact_token,
    portainer_connector,
    portainer_source,
)

# ---------------------------------------------------------------------------
# Synthetic Test Fixtures
# ---------------------------------------------------------------------------

SAMPLE_ENDPOINTS = [
    {
        "Id": 1,
        "Name": "Primary Docker Cluster",
        "Type": 1,
        "URL": "tcp://docker-primary.internal:2375",
        "Status": 1,
        "Snapshots": [{"DockerVersion": "24.0.7"}],
    },
    {
        "Id": 2,
        "Name": "Edge Node Alpha",
        "Type": 4,
        "URL": "https://edge-node.internal:8000",
        "Status": 1,
        "Snapshots": [{"DockerVersion": "25.0.3"}],
    },
]

SAMPLE_STACKS = [
    {
        "Id": 10,
        "Name": "redis-broker",
        "EndpointId": 1,
        "Type": 2,
        "Status": 1,
        "CreatedBy": "devops-lead",
    },
    {
        "Id": 11,
        "Name": "api-gateway",
        "EndpointId": 1,
        "Type": 2,
        "Status": 1,
        "CreatedBy": "admin",
    },
]

SAMPLE_STACK_FILE = {
    "StackFileContent": (
        "version: '3.8'\n"
        "services:\n"
        "  redis:\n"
        "    image: redis:7-alpine\n"
        "    ports:\n"
        "      - '6379:6379'\n"
    )
}

SAMPLE_CONTAINERS = [
    {
        "Id": "abc1234567890def",
        "Names": ["/redis-master"],
        "Image": "redis:7-alpine",
        "State": "running",
        "Status": "Up 4 days (healthy)",
        "Ports": [{"PublicPort": 6379, "PrivatePort": 6379, "Type": "tcp"}],
        "Mounts": [{"Source": "/var/lib/redis", "Destination": "/data"}],
    },
    {
        "Id": "def9876543210abc",
        "Names": ["/api-proxy"],
        "Image": "nginx:1.25",
        "State": "running",
        "Status": "Up 2 hours",
        "Ports": [{"PublicPort": 80, "PrivatePort": 80, "Type": "tcp"}],
        "Mounts": [],
    },
]


class MockTransport(httpx.BaseTransport):
    """Mock HTTP transport returning synthetic Portainer responses."""

    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.call_count = 0
        self.last_request = None

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.call_count += 1
        self.last_request = request

        if self.status_code != 200:
            return httpx.Response(status_code=self.status_code, text="Simulated server error")

        path = request.url.path
        if "/api/auth" in path:
            return httpx.Response(200, json={"jwt": "synthetic_jwt_token_12345"})
        if "/api/endpoints" in path and "/docker/containers/json" in path:
            return httpx.Response(200, json=SAMPLE_CONTAINERS)
        if "/api/endpoints" in path:
            return httpx.Response(200, json=SAMPLE_ENDPOINTS)
        if "/api/stacks/" in path and "/file" in path:
            return httpx.Response(200, json=SAMPLE_STACK_FILE)
        if "/api/stacks" in path:
            return httpx.Response(200, json=SAMPLE_STACKS)

        return httpx.Response(200, json={})


# ---------------------------------------------------------------------------
# Helper & Redaction Tests
# ---------------------------------------------------------------------------


def test_redact_token() -> None:
    secret = "secret_portainer_api_key_xyz"
    msg = f"Request failed with {secret} on port 9443"
    redacted = _redact_token(msg, secret)

    assert secret not in redacted
    assert "[REDACTED_PORTAINER_CREDENTIAL]" in redacted


def test_endpoint_type_name() -> None:
    assert _endpoint_type_name(1) == "Docker Standalone"
    assert _endpoint_type_name(2) == "Docker Swarm Agent"
    assert _endpoint_type_name(4) == "Edge Agent Docker"


# ---------------------------------------------------------------------------
# Client Tests
# ---------------------------------------------------------------------------


def test_client_authenticate_with_password() -> None:
    transport = MockTransport()
    client = httpx.Client(transport=transport)
    portainer_client = PortainerClient(
        "https://portainer.local:9443",
        username="admin",
        password="secretpassword",
        client=client,
    )

    portainer_client.authenticate()
    assert portainer_client.jwt_token == "synthetic_jwt_token_12345"
    assert transport.call_count == 1


def test_client_auth_error_401() -> None:
    transport = MockTransport(status_code=401)
    client = httpx.Client(transport=transport)
    portainer_client = PortainerClient(
        "https://portainer.local:9443",
        api_key="invalid_key",
        client=client,
    )

    with pytest.raises(PortainerAuthError) as exc_info:
        portainer_client.get_endpoints()

    assert "authentication rejected" in str(exc_info.value)


def test_client_rate_limit_error_429() -> None:
    transport = MockTransport(status_code=429)
    client = httpx.Client(transport=transport)
    portainer_client = PortainerClient(
        "https://portainer.local:9443",
        api_key="valid_key",
        client=client,
    )

    with pytest.raises(PortainerRateLimitError):
        portainer_client.get_endpoints()


def test_client_get_endpoints() -> None:
    transport = MockTransport()
    client = httpx.Client(transport=transport)
    portainer_client = PortainerClient("https://portainer.local:9443", client=client)

    endpoints = portainer_client.get_endpoints()
    assert len(endpoints) == 2
    assert endpoints[0]["Name"] == "Primary Docker Cluster"


def test_client_get_stacks_and_file() -> None:
    transport = MockTransport()
    client = httpx.Client(transport=transport)
    portainer_client = PortainerClient("https://portainer.local:9443", client=client)

    stacks = portainer_client.get_stacks()
    assert len(stacks) == 2
    assert stacks[0]["Name"] == "redis-broker"

    file_content = portainer_client.get_stack_file(10)
    assert "services:" in file_content
    assert "redis:7-alpine" in file_content


def test_client_get_containers() -> None:
    transport = MockTransport()
    client = httpx.Client(transport=transport)
    portainer_client = PortainerClient("https://portainer.local:9443", client=client)

    containers = portainer_client.get_containers(1)
    assert len(containers) == 2
    assert containers[0]["Image"] == "redis:7-alpine"
    assert containers[0]["State"] == "running"


# ---------------------------------------------------------------------------
# Markdown Formatting Tests
# ---------------------------------------------------------------------------


def test_build_environments_markdown() -> None:
    md = _build_environments_markdown(SAMPLE_ENDPOINTS)

    assert "# Portainer Managed Environments & Cluster Inventory" in md
    assert "Primary Docker Cluster" in md
    assert "Edge Node Alpha" in md
    assert "24.0.7" in md


def test_build_stacks_markdown() -> None:
    files = {10: SAMPLE_STACK_FILE["StackFileContent"]}
    md = _build_stacks_markdown(SAMPLE_STACKS, files)

    assert "# Portainer Stacks & Compose Topology" in md
    assert "## Stack: redis-broker" in md
    assert "redis:7-alpine" in md
    assert "Created By**: devops-lead" in md


def test_build_containers_markdown() -> None:
    md = _build_containers_markdown("Primary Cluster", 1, SAMPLE_CONTAINERS)

    assert "# Container Runtime State: Primary Cluster" in md
    assert "Container: /redis-master" in md
    assert "Port Forwarding**: 6379->6379/tcp" in md
    assert "Volume Mounts**: `/var/lib/redis` -> `/data`" in md


# ---------------------------------------------------------------------------
# DLT Source Invariant & Integration Tests
# ---------------------------------------------------------------------------


def test_portainer_source_document_mode_invariant() -> None:
    transport = MockTransport()
    fake_client = PortainerClient(
        "https://portainer.local:9443",
        client=httpx.Client(transport=transport),
    )

    source = portainer_source(api_url="https://portainer.local:9443", client=fake_client)

    # Invariant: Must declare DOCUMENT_SOURCE_ATTR = "portainer"
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == PORTAINER_SOURCE_NAME


def test_portainer_source_yields_expected_reports() -> None:
    transport = MockTransport()
    fake_client = PortainerClient(
        "https://portainer.local:9443",
        client=httpx.Client(transport=transport),
    )

    source = portainer_source(
        api_url="https://portainer.local:9443",
        include_containers=True,
        include_stacks=True,
        client=fake_client,
    )

    resources = list(source.selected_resources.values())
    assert len(resources) == 1
    resource = resources[0]
    assert resource.name == PORTAINER_TABLE_NAME
    assert resource.write_disposition == "replace"

    rows = list(resource())
    # 1 environments + 1 stacks + 2 container catalogs (for 2 endpoints) = 4
    assert len(rows) == 4

    report_types = [r["metadata"]["report_type"] for r in rows]
    assert "environments" in report_types
    assert "stacks" in report_types
    assert "containers" in report_types

    for r in rows:
        assert r["metadata"]["source"] == PORTAINER_SOURCE_NAME
        assert len(r["id"]) > 0
        assert r["text"].startswith("# ")


def test_portainer_source_deterministic_id_stability() -> None:
    transport = MockTransport()
    fake_client = PortainerClient(
        "https://portainer.local:9443",
        client=httpx.Client(transport=transport),
    )

    source_1 = portainer_source(api_url="https://portainer.local:9443", client=fake_client)
    rows_1 = list(next(iter(source_1.selected_resources.values()))())

    source_2 = portainer_source(api_url="https://portainer.local:9443", client=fake_client)
    rows_2 = list(next(iter(source_2.selected_resources.values()))())

    # Deterministic IDs ensure repeated runs don't re-cognify identical reports
    assert [r["id"] for r in rows_1] == [r["id"] for r in rows_2]


def test_portainer_source_filters_by_endpoint_ids() -> None:
    transport = MockTransport()
    fake_client = PortainerClient(
        "https://portainer.local:9443",
        client=httpx.Client(transport=transport),
    )

    source = portainer_source(
        api_url="https://portainer.local:9443",
        endpoint_ids=[1],
        client=fake_client,
    )

    resource = next(iter(source.selected_resources.values()))
    rows = list(resource())

    container_rows = [r for r in rows if r["metadata"]["report_type"] == "containers"]
    assert len(container_rows) == 1
    assert container_rows[0]["metadata"]["endpoint_id"] == 1


@pytest.mark.asyncio
async def test_portainer_connector_async_wrapper() -> None:
    transport = MockTransport()
    fake_client = PortainerClient(
        "https://portainer.local:9443",
        client=httpx.Client(transport=transport),
    )

    config = PortainerConfig(
        api_url="https://portainer.local:9443",
        api_key="test_key",
    )

    documents = await portainer_connector(config, client=fake_client)
    assert len(documents) == 4
    for doc in documents:
        assert "id" in doc
        assert "text" in doc
        assert doc["metadata"]["source"] == "portainer"
