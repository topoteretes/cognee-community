"""Offline HTTP fixtures and isolated dlt pipelines."""

import os
from copy import deepcopy
from types import SimpleNamespace

os.environ["RUNTIME__DLTHUB_TELEMETRY"] = "false"
os.environ["ENABLE_TELEMETRY"] = "false"
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "true"

import httpx
import pytest


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    import socket

    from dlt.common.configuration.container import Container
    from dlt.common.pipeline import PipelineContext

    def forbidden(*args, **kwargs):
        raise AssertionError("Network access forbidden in Metabase tests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    for name in ("METABASE_URL", "METABASE_API_KEY", "METABASE_USERNAME", "METABASE_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    Container()[PipelineContext].deactivate()
    yield
    Container()[PipelineContext].deactivate()


@pytest.fixture
def metabase_api():
    stamp = "2026-01-01T00:00:00Z"
    data = {
        "collection": [
            {"id": 1, "name": "Finance", "description": "Team metrics", "updated_at": stamp}
        ],
        "card": [
            {
                "id": 1,
                "name": "Revenue",
                "description": "Monthly sales",
                "updated_at": stamp,
                "dataset_query": {
                    "type": "native",
                    "native": {"query": "SELECT sum(total) FROM orders"},
                },
            }
        ],
        "dashboard": [
            {
                "id": 1,
                "name": "Overview",
                "description": "Executive report",
                "updated_at": stamp,
                "dashcards": [{"card": {"id": 1, "name": "Revenue"}}],
            }
        ],
    }
    requests = []
    failures = {}

    def respond(request):
        requests.append(request)
        path = request.url.path.removeprefix("/metabase/api/")
        if failures.get(path):
            return httpx.Response(failures[path].pop(0), headers={"Retry-After": "0"})
        if path == "session":
            if request.method == "POST":
                return httpx.Response(200, json={"id": "session-token"})
            assert request.headers["X-Metabase-Session"] == "session-token"
            return httpx.Response(204)
        assert (
            request.headers.get("x-api-key") == "test-key"
            or request.headers.get("X-Metabase-Session") == "session-token"
        )
        parts = path.split("/")
        if len(parts) == 1:
            return httpx.Response(200, json=deepcopy(data[path]))
        item = next(x for x in data[parts[0]] if str(x["id"]) == parts[1])
        return httpx.Response(200, json=deepcopy(item))

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        yield SimpleNamespace(client=client, data=data, requests=requests, failures=failures)


@pytest.fixture
def pipeline_factory(tmp_path):
    import dlt

    def create():
        return dlt.pipeline(
            pipeline_name="metabase_test",
            destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'metabase.db'}"),
            dataset_name="metabase_ds",
            pipelines_dir=str(tmp_path / "state"),
        )

    return create
