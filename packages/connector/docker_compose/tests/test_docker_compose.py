"""Unit tests for the Docker Compose infrastructure data-source connector.

Tests use self-contained YAML compose fixtures generated in ``tmp_path``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import yaml

from cognee_community_connector_docker_compose.docker_compose import (
    DOCUMENT_SOURCE_ATTR,
    DOCKER_COMPOSE_SOURCE_NAME,
    DOCKER_COMPOSE_TABLE_NAME,
    docker_compose_source,
    iter_docker_compose_services,
)

SAMPLE_COMPOSE = {
    "version": "3.8",
    "services": {
        "web": {
            "build": {"context": ".", "dockerfile": "Dockerfile.web"},
            "container_name": "app-web-frontend",
            "ports": ["3000:3000"],
            "environment": {
                "NODE_ENV": "production",
                "API_URL": "http://api:8080",
            },
            "depends_on": {
                "api": {"condition": "service_healthy"}
            },
            "networks": ["frontend-network", "backend-network"],
        },
        "api": {
            "image": "my-org/api:v1.2.0",
            "ports": ["8080:8080"],
            "environment": [
                "DATABASE_URL=postgresql://user:pass@db:5432/app",
                "REDIS_URL=redis://redis:6379",
            ],
            "depends_on": ["db", "redis"],
            "restart": "always",
            "healthcheck": {
                "test": ["CMD-SHELL", "curl -f http://localhost:8080/health || exit 1"],
                "interval": "30s",
                "timeout": "10s",
                "retries": 3,
            },
            "networks": ["backend-network"],
        },
        "db": {
            "image": "postgres:15-alpine",
            "ports": ["5432:5432"],
            "environment": {
                "POSTGRES_DB": "app",
                "POSTGRES_USER": "user",
            },
            "volumes": ["postgres_data:/var/lib/postgresql/data"],
            "networks": ["backend-network"],
        },
        "redis": {
            "image": "redis:7-alpine",
            "ports": ["6379:6379"],
            "networks": ["backend-network"],
        },
    },
    "networks": {
        "frontend-network": {},
        "backend-network": {},
    },
    "volumes": {
        "postgres_data": {},
    },
}


def test_iter_docker_compose_services(tmp_path):
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text(yaml.dump(SAMPLE_COMPOSE))

    docs = list(iter_docker_compose_services(compose_file))
    assert len(docs) == 4

    web_doc = next(d for d in docs if d["service_name"] == "web")
    assert web_doc["id"] == "docker_compose:service:web"
    assert "Context: ., Dockerfile: Dockerfile.web" in web_doc["content"]
    assert "3000:3000" in web_doc["content"]
    assert "api (condition: service_healthy)" in web_doc["content"]

    api_doc = next(d for d in docs if d["service_name"] == "api")
    assert api_doc["id"] == "docker_compose:service:api"
    assert "my-org/api:v1.2.0" in api_doc["content"]
    assert "Restart Policy: always" in api_doc["content"]
    assert "Healthcheck: CMD-SHELL curl -f http://localhost:8080/health || exit 1" in api_doc["content"]
    assert "db" in api_doc["depends_on"]
    assert "redis" in api_doc["depends_on"]

    db_doc = next(d for d in docs if d["service_name"] == "db")
    assert "postgres_data:/var/lib/postgresql/data" in db_doc["content"]
    assert "5432:5432" in db_doc["content"]


def test_orphan_cleanup_simulation(tmp_path):
    """Removing a service from docker-compose.yml drops it from the yielded records."""
    compose_v1 = tmp_path / "v1-docker-compose.yml"
    compose_v1.write_text(yaml.dump(SAMPLE_COMPOSE))

    # Create v2 without redis
    v2_data = yaml.safe_load(yaml.dump(SAMPLE_COMPOSE))
    del v2_data["services"]["redis"]
    compose_v2 = tmp_path / "v2-docker-compose.yml"
    compose_v2.write_text(yaml.dump(v2_data))

    docs_v1 = list(iter_docker_compose_services(compose_v1))
    docs_v2 = list(iter_docker_compose_services(compose_v2))

    names_v1 = {d["service_name"] for d in docs_v1}
    names_v2 = {d["service_name"] for d in docs_v2}

    assert "redis" in names_v1
    assert "redis" not in names_v2


def test_directory_with_compose_files(tmp_path):
    sub = tmp_path / "project"
    sub.mkdir()
    (sub / "docker-compose.yml").write_text(yaml.dump(SAMPLE_COMPOSE))

    docs = list(iter_docker_compose_services(sub))
    assert len(docs) == 4


def test_missing_and_corrupt_files(tmp_path):
    with pytest.raises(FileNotFoundError):
        list(iter_docker_compose_services(tmp_path / "nonexistent-compose.yml"))

    corrupt = tmp_path / "docker-compose.yml"
    corrupt.write_text(": : bad yaml ::")
    assert list(iter_docker_compose_services(corrupt)) == []


def test_docker_compose_source_resource():
    mock_dlt = MagicMock()

    def mock_resource_decorator(*args, **kwargs):
        def decorator(fn):
            wrapper = MagicMock()
            wrapper.__name__ = fn.__name__
            wrapper._kwargs = kwargs
            return wrapper
        return decorator

    mock_dlt.resource = mock_resource_decorator

    with patch.dict("sys.modules", {"dlt": mock_dlt}):
        resource = docker_compose_source("/dummy/docker-compose.yml")

        assert getattr(resource, DOCUMENT_SOURCE_ATTR) == DOCKER_COMPOSE_SOURCE_NAME
        assert resource._kwargs.get("name") == DOCKER_COMPOSE_TABLE_NAME
        assert resource._kwargs.get("primary_key") == "id"
        assert resource._kwargs.get("write_disposition") == "replace"
