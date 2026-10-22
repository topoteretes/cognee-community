"""Tests for Fly.io document rendering and walker row generation."""

import pytest
from cognee_community_connector_flyio.flyio import (
    render_app,
    render_machine,
    render_volume,
    flyio_source,
    _iter_rows,
)
from fake_flyio import FakeFlyio


def test_render_app():
    app = {
        "id": "app_gateway",
        "name": "gateway",
        "status": "deployed",
        "organization": {"slug": "acme-corp"},
        "current_release": {
            "version": 5,
            "description": "Bump base image",
        },
    }
    doc = render_app(app)
    assert doc["id"] == "app:gateway"
    assert "Fly.io App: gateway [deployed]" in doc["title"]
    assert "Organization: acme-corp" in doc["content"]
    assert "Current Release Version: v5" in doc["content"]
    assert doc["_deleted"] is False


def test_render_machine():
    mach = {
        "id": "148ed21a070989",
        "name": "winter-snow-42",
        "state": "started",
        "region": "iad",
        "config": {
            "image": "registry.fly.io/gateway:v5",
            "guest": {"cpus": 4, "memory_mb": 4096},
        },
        "checks": [{"name": "health", "status": "passing"}],
    }
    doc = render_machine("gateway", mach)
    assert doc["id"] == "machine:gateway:148ed21a070989"
    assert "Fly.io Machine: gateway / winter-snow-42 (148ed21a070989) [started]" in doc["title"]
    assert "Region: iad" in doc["content"]
    assert "4 CPU, 4096 MB RAM" in doc["content"]
    assert doc["_deleted"] is False


def test_render_volume():
    vol = {
        "id": "vol_data_01",
        "name": "pgdata",
        "region": "iad",
        "size_gb": 50,
        "state": "created",
        "encrypted": True,
        "attached_machine_id": "148ed21a070989",
    }
    doc = render_volume("gateway", vol)
    assert doc["id"] == "volume:gateway:vol_data_01"
    assert "Fly.io Volume: gateway / pgdata (vol_data_01)" in doc["title"]
    assert "Size (GB): 50" in doc["content"]
    assert "Attached Machine: 148ed21a070989" in doc["content"]
    assert doc["_deleted"] is False


def test_iter_rows_walker():
    fake = FakeFlyio()
    fake.add_app("api-backend")
    fake.add_machine("api-backend", "m1", "mach-1", region="iad")
    fake.add_volume("api-backend", "v1", "vol-1", region="iad")

    state = {}
    stats = {}
    rows = list(_iter_rows(fake, state, stats, include=("apps", "machines", "volumes")))

    assert len(rows) == 3
    ids = {r["id"] for r in rows}
    assert "app:api-backend" in ids
    assert "machine:api-backend:m1" in ids
    assert "volume:api-backend:v1" in ids
    assert stats["scanned"] == 3
    assert stats["skipped"] == 0


def test_source_validation():
    with pytest.raises(ValueError, match="non-empty resource_name"):
        flyio_source(resource_name="", api_key="FlyV1 12345678")

    with pytest.raises(ValueError, match="requires an api_key"):
        flyio_source(resource_name="test")

    with pytest.raises(ValueError, match="non-empty subset"):
        flyio_source(resource_name="test", api_key="FlyV1 12345678", include=("invalid_kind",))
