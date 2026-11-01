"""Hermetic unit tests for the Cognee DigitalOcean Cloud Connector."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

# Ensure test and package root are in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx
import pytest
from fake_digitalocean import FAKE_TOKEN, create_fake_client

from cognee_community_connector_digitalocean.cli import main as cli_main
from cognee_community_connector_digitalocean.digitalocean import (
    DigitalOceanClient,
    DigitalOceanConnectorError,
    digitalocean_source,
    render_app,
    render_database,
    render_droplet,
    render_kubernetes_cluster,
    render_project,
    sync_digitalocean_records,
)


def test_client_missing_token_raises():
    with pytest.raises(ValueError):
        DigitalOceanClient(token="")


def test_client_auth_success():
    client = create_fake_client()
    data = client.request("droplets")
    assert "droplets" in data
    assert len(data["droplets"]) == 2


def test_client_unauthorized():
    client = create_fake_client(token="invalid_token")
    with pytest.raises(DigitalOceanConnectorError) as exc_info:
        client.request("droplets")
    assert exc_info.value.status_code == 401


def test_client_rate_limit_backoff(monkeypatch):
    calls = []

    def mock_get(*args, **kwargs):
        if len(calls) == 0:
            calls.append(1)
            return httpx.Response(status_code=429, headers={"Ratelimit-Reset": "1"})
        return httpx.Response(status_code=200, json={"droplets": []})

    client = create_fake_client()
    monkeypatch.setattr(client._client, "get", mock_get)
    monkeypatch.setattr("time.sleep", lambda s: None)

    data = client.request("droplets")
    assert "droplets" in data
    assert len(calls) == 1


def test_render_droplet():
    droplet = {
        "id": 1001,
        "name": "api-gateway-01",
        "status": "active",
        "region": {"slug": "ams3"},
        "size_slug": "s-2vcpu-4gb",
        "vcpus": 2,
        "memory": 4096,
        "disk": 80,
        "created_at": "2026-10-01T12:00:00Z",
        "tags": ["web", "api"],
        "networks": {"v4": [{"ip_address": "178.62.200.10"}]},
        "image": {"distribution": "Ubuntu", "name": "24.04 LTS"},
    }
    doc = render_droplet(droplet)
    assert doc["id"] == "do:droplet:1001"
    assert doc["title"] == "Droplet: api-gateway-01 (active)"
    assert doc["status"] == "active"
    assert doc["region"] == "ams3"
    assert "178.62.200.10" in doc["content"]
    assert "Ubuntu 24.04 LTS" in doc["content"]


def test_render_app_with_deployments():
    app = {
        "id": "app-uuid-1234",
        "spec": {"name": "microservice-auth", "region": "nyc"},
        "live_url": "https://auth.ondigitalocean.app",
        "active_deployment": {"phase": "ACTIVE"},
        "updated_at": "2026-10-20T10:00:00Z",
    }
    deployments = [
        {
            "id": "dep-101",
            "phase": "ACTIVE",
            "cause": "Git commit",
            "created_at": "2026-10-20T09:50:00Z",
            "progress": {"summary": "Build completed"},
        }
    ]
    doc = render_app(app, deployments)
    assert doc["id"] == "do:app:app-uuid-1234"
    assert "microservice-auth" in doc["content"]
    assert "Active Deployment Phase: ACTIVE" in doc["content"]
    assert "Build completed" in doc["content"]


def test_render_database_strictly_redacts_credentials():
    db = {
        "id": "db-pg-cluster",
        "name": "user-database",
        "engine": "pg",
        "version": "16",
        "num_nodes": 2,
        "size": "db-s-2vcpu-4gb",
        "region": "nyc3",
        "status": "online",
        "created_at": "2026-09-10T10:00:00Z",
        # Sensitive credentials:
        "connection": {
            "uri": "postgres://doadmin:LEAKED_PASSWORD@db.ondigitalocean.com:25060/db",
            "password": "LEAKED_PASSWORD",
        },
    }
    doc = render_database(db)
    assert doc["id"] == "do:database:db-pg-cluster"
    assert "user-database" in doc["content"]
    assert "Engine: pg v16" in doc["content"]

    # Critical security assertions:
    assert "LEAKED_PASSWORD" not in doc["content"]
    assert "postgres://doadmin" not in doc["content"]
    assert "passwords and credentials safely redacted" in doc["content"]


def test_render_kubernetes_cluster():
    cluster = {
        "id": "k8s-cluster-123",
        "name": "production-k8s",
        "region": "fra1",
        "version": "1.30.2-do.0",
        "status": {"state": "running"},
        "node_pools": [{"count": 4}],
    }
    doc = render_kubernetes_cluster(cluster)
    assert doc["id"] == "do:k8s:k8s-cluster-123"
    assert "production-k8s" in doc["content"]
    assert "Kubernetes Version: 1.30.2-do.0" in doc["content"]
    assert "4 total nodes" in doc["content"]


def test_render_project_with_resources():
    proj = {
        "id": "proj-core-999",
        "name": "Production Core",
        "description": "Production servers and clusters",
        "purpose": "Backend",
        "is_default": True,
    }
    resources = [{"urn": "do:droplet:1001"}]
    doc = render_project(proj, resources)
    assert doc["id"] == "do:project:proj-core-999"
    assert "Production Core" in doc["content"]
    assert "Resource: do:droplet:1001" in doc["content"]


def test_sync_digitalocean_records_end_to_end():
    client = create_fake_client()
    records = list(sync_digitalocean_records(client))

    # 1 project + 2 droplets + 1 app + 1 database + 1 k8s = 6 records
    assert len(records) == 6

    types = {r["type"] for r in records}
    assert types == {"project", "droplet", "app", "database", "kubernetes"}

    ids = {r["id"] for r in records}
    assert "do:project:proj_default_01" in ids
    assert "do:droplet:3164444" in ids
    assert "do:droplet:3164445" in ids
    assert "do:app:app_99112233-4455-6677" in ids
    assert "do:database:db_55667788" in ids
    assert "do:k8s:k8s_cluster_alpha" in ids


def test_cli_execution_with_token(monkeypatch, capsys):
    fake_client = create_fake_client()
    with patch(
        "cognee_community_connector_digitalocean.cli.DigitalOceanClient", return_value=fake_client
    ):
        monkeypatch.setattr("sys.argv", ["cognee-digitalocean", "--token", FAKE_TOKEN])
        cli_main()
        captured = capsys.readouterr()
        assert "[SUCCESS] Ingested 6 DigitalOcean document(s)" in captured.out
        assert "droplet: 2" in captured.out
        assert "database: 1" in captured.out
        assert "kubernetes: 1" in captured.out


def test_cli_missing_token_exits(monkeypatch, capsys):
    monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
    monkeypatch.setattr("sys.argv", ["cognee-digitalocean", "--token", ""])
    with pytest.raises(SystemExit) as exc:
        cli_main()
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "DigitalOcean token required" in captured.err


def test_digitalocean_source_execution():
    fake_client = create_fake_client()
    with patch(
        "cognee_community_connector_digitalocean.digitalocean.DigitalOceanClient",
        return_value=fake_client,
    ):
        source = digitalocean_source(token=FAKE_TOKEN)
        assert source is not None
        if isinstance(source, list) and callable(source[0]):
            docs = list(source[0]())
        else:
            docs = list(source)
        assert len(docs) == 6


def test_clean_text_strips_ansi_and_control_characters():
    from cognee_community_connector_digitalocean.digitalocean import _clean_text

    raw_text = "\x1b[31mRed Alert\x1b[0m with NUL \x00 byte and \x07 bell"
    cleaned = _clean_text(raw_text)
    assert "\x1b[31m" not in cleaned
    assert "\x00" not in cleaned
    assert "\x07" not in cleaned
    assert "Red Alert with NUL  byte and  bell" in cleaned
