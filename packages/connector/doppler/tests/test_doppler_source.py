"""Tests for Doppler document rendering and walker row generation."""

import pytest
from cognee_community_connector_doppler.doppler import (
    render_project,
    render_environment,
    render_config,
    render_log,
    doppler_source,
    _iter_rows,
)
from fake_doppler import FakeDoppler


def test_render_project():
    project = {
        "id": "billing-api",
        "name": "billing-api",
        "description": "Payment microservice",
        "created_at": "2026-10-01T10:00:00Z",
    }
    doc = render_project(project)
    assert doc["id"] == "project:billing-api"
    assert "Project: billing-api" in doc["title"]
    assert "Payment microservice" in doc["content"]
    assert doc["_deleted"] is False


def test_render_config_keys_only():
    config = {
        "name": "prd_web",
        "project": "web-app",
        "environment": "prd",
        "root": True,
        "locked": False,
    }
    variable_keys = ["DATABASE_URL", "REDIS_HOST", "STRIPE_SECRET"]
    doc = render_config(config, variable_keys)
    assert doc["id"] == "config:web-app:prd_web"
    assert "Config: web-app / prd_web [prd]" in doc["title"]
    assert "DATABASE_URL, REDIS_HOST, STRIPE_SECRET" in doc["content"]
    assert "Configured Variable Keys Count: 3" in doc["content"]
    assert doc["_deleted"] is False


def test_iter_rows_walker():
    fake = FakeDoppler()
    fake.add_project("core-service")
    fake.add_environment("core-service", "prd", "Production")
    fake.add_config("core-service", "prd_main", "prd", ["PORT", "LOG_LEVEL"])
    fake.add_log("core-service", "Added LOG_LEVEL variable")

    state = {}
    stats = {}
    rows = list(_iter_rows(fake, state, stats, include=("projects", "environments", "configs", "logs")))

    assert len(rows) == 4
    ids = {r["id"] for r in rows}
    assert "project:core-service" in ids
    assert "environment:core-service:prd" in ids
    assert "config:core-service:prd_main" in ids
    assert any(i.startswith("log:core-service:") for i in ids)
    assert stats["scanned"] == 4
    assert stats["skipped"] == 0


def test_source_validation():
    with pytest.raises(ValueError, match="non-empty resource_name"):
        doppler_source(resource_name="", api_key="dp.st.123")

    with pytest.raises(ValueError, match="requires an api_key"):
        doppler_source(resource_name="test")

    with pytest.raises(ValueError, match="non-empty subset"):
        doppler_source(resource_name="test", api_key="dp.st.123", include=("invalid_kind",))
