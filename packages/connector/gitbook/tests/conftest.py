"""Offline HTTP response fixtures and isolated dlt pipelines, like Metabase."""

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
        raise AssertionError("Network access forbidden in GitBook tests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    for name in ("GITBOOK_API_TOKEN", "GITBOOK_ORG_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    Container()[PipelineContext].deactivate()
    yield
    Container()[PipelineContext].deactivate()


@pytest.fixture
def gitbook_api():
    # Minimal response projections from the official OpenAPI schemas (API notes).
    # Served in-process by MockTransport; never opens a socket.
    child = {
        "id": "child",
        "type": "document",
        "title": "Install",
        "path": "guide/install",
        "pages": [],
    }
    page = {"id": "page", "type": "document", "title": "Guide", "path": "guide", "pages": [child]}
    data = {
        "/v1/orgs": {"items": [{"id": "org", "title": "Team"}]},
        "/v1/orgs/org/sites": {
            "items": [
                {
                    "id": "site",
                    "title": "Docs",
                    "visibility": "public",
                    "siteSpaces": 2,
                    "urls": {
                        "app": "https://app.gitbook.com/site",
                        "published": "https://docs.test",
                    },
                }
            ]
        },
        "/v1/orgs/org/sites/site/site-spaces": {
            "items": [
                {"id": "ss1", "space": {"id": "space"}},
                {"id": "ss2", "space": {"id": "other"}},
            ]
        },
        "/v1/spaces/space": {"id": "space", "title": "Manual"},
        "/v1/spaces/other": {"id": "other", "title": "Reference"},
        "/v1/spaces/space/content": {"pages": [page]},
        "/v1/spaces/other/content": {"pages": []},
        "/v1/spaces/space/content/page/page": {**page, "markdown": "Welcome **reader**"},
        "/v1/spaces/space/content/page/child": {**child, "markdown": "Run `pip install demo`"},
    }
    requests, failures, responses = [], {}, {}

    def respond(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer test-token"
        assert "token" not in str(request.url)
        path = request.url.path
        if failures.get(path):
            return httpx.Response(failures[path].pop(0), headers={"Retry-After": "0"})
        if str(request.url) in responses:
            return responses[str(request.url)]
        if path in responses:
            return responses[path]
        assert path in data, f"Unexpected API request: {path}"
        if "/content/page/" in path:
            assert request.url.params["format"] == "markdown"
        return httpx.Response(200, json=deepcopy(data[path]))

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        yield SimpleNamespace(
            client=client, data=data, requests=requests, failures=failures, responses=responses
        )


@pytest.fixture
def pipeline_factory(tmp_path):
    import dlt

    def create():
        return dlt.pipeline(
            pipeline_name="gitbook_test",
            destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'gitbook.db'}"),
            dataset_name="gitbook_ds",
            pipelines_dir=str(tmp_path / "state"),
        )

    return create
