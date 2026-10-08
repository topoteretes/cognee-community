"""GitBook snapshots and forget-on-delete without network or LLM credentials."""

import asyncio
import importlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import NAMESPACE_OID, uuid5

import httpx
import pytest
from cognee.tasks.ingestion.dlt_utils import document_source_tag
from dlt.pipeline.exceptions import PipelineStepFailed

from cognee_community_connector_gitbook import (
    GitBookAuthError,
    GitBookError,
    GitBookResponseError,
    gitbook_source,
)
from cognee_community_connector_gitbook.gitbook import _API, _sync

BASE_URL = "https://example.test"
SITES = "/v1/orgs/org/sites"
SPACES = SITES + "/site/site-spaces"
CONTENT = "/v1/spaces/space/content"
PAGE = "page:org:site:space:page"
CHILD = "page:org:site:space:child"


def source(api, **kwargs):
    return gitbook_source(
        client=api.client,
        request_interval=0,
        **({"api_token": "test-token", "base_url": BASE_URL} | kwargs),
    )


def sync(api, org_id=None):
    return _sync(_API(api.client, BASE_URL, "test-token"), org_id)


def read_rows(pipeline):
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, title, content, url FROM gitbook_documents") as cursor,
    ):
        return {
            row[0]: dict(zip(("id", "title", "content", "url"), row, strict=True))
            for row in cursor.fetchall()
        }


def test_initial_full_ingest(gitbook_api, pipeline_factory):
    pipeline = pipeline_factory()
    src = source(gitbook_api)
    assert document_source_tag(src) == "gitbook"
    pipeline.run(src)
    rows = read_rows(pipeline)
    assert set(rows) == {"gitbook:source", "site:org:site", PAGE, CHILD}
    assert "Welcome **reader**" in rows[PAGE]["content"]
    assert "Parent page: page" in rows[CHILD]["content"]
    assert "Path: guide/install" in rows[CHILD]["content"]
    assert "Space: Manual\nSite: Docs\nOrganization: org" in rows[PAGE]["content"]
    assert any(r.url.path == "/v1/spaces/other/content" for r in gitbook_api.requests)
    assert not gitbook_api.client.is_closed


def test_org_discovery(gitbook_api):
    gitbook_api.data["/v1/orgs"]["items"].append({"id": "second"})
    gitbook_api.data["/v1/orgs/second/sites"] = {"items": []}
    sync(gitbook_api)
    assert gitbook_api.requests[0].url.path == "/v1/orgs"
    assert any(r.url.path == "/v1/orgs/second/sites" for r in gitbook_api.requests)


def test_given_org(gitbook_api):
    sync(gitbook_api, "org")
    assert all(r.url.path != "/v1/orgs" for r in gitbook_api.requests)


def test_environment_configuration(monkeypatch, gitbook_api):
    monkeypatch.setenv("GITBOOK_API_TOKEN", "test-token")
    monkeypatch.setenv("GITBOOK_ORG_ID", "org")
    assert list(source(gitbook_api, api_token=None))
    assert gitbook_api.requests[0].url.path == SITES


def test_auth_failure(gitbook_api):
    gitbook_api.failures["/v1/orgs"] = [401]
    with pytest.raises(GitBookAuthError, match="authentication failed"):
        sync(gitbook_api)


def test_summary(gitbook_api):
    row = next(r for r in sync(gitbook_api) if r["id"] == "site:org:site")
    assert row["title"] == "Docs"
    assert row["url"] == "https://docs.test"
    assert "Visibility: public" in row["content"]


@pytest.mark.parametrize("empty", ["site", "space"])
def test_empty_content(empty, gitbook_api):
    if empty == "site":
        gitbook_api.data[SPACES]["items"] = []
    else:
        gitbook_api.data[CONTENT]["pages"] = []
    assert {r["id"] for r in sync(gitbook_api)} == {"gitbook:source", "site:org:site"}


@pytest.mark.parametrize("endpoint", ["/v1/orgs", SITES, SPACES])
def test_pagination_exhausts(endpoint, gitbook_api):
    original = gitbook_api.data[endpoint]
    gitbook_api.data[endpoint] = {"items": [], "next": {"page": "next cursor"}}
    gitbook_api.responses[BASE_URL + endpoint + "?page=next+cursor"] = httpx.Response(
        200, json=original
    )
    assert len(sync(gitbook_api)) == 4
    assert any(r.url.params.get("page") == "next cursor" for r in gitbook_api.requests)


def test_link_pagination(gitbook_api):
    gitbook_api.responses[SITES] = httpx.Response(
        200, json={"items": []}, headers={"Link": f'<{SITES}?page=2&all=true>; rel="next"'}
    )
    gitbook_api.responses[BASE_URL + SITES + "?page=2&all=true"] = httpx.Response(
        200, json=gitbook_api.data[SITES]
    )
    assert len(sync(gitbook_api)) == 4


@pytest.mark.parametrize("link", ["https://evil.test/v1/orgs", "/v1/orgs?api_token=secret"])
def test_unsafe_pagination_rejected(link, gitbook_api):
    gitbook_api.responses["/v1/orgs"] = httpx.Response(
        200, json={"items": []}, headers={"Link": f'<{link}>; rel="next"'}
    )
    with pytest.raises(GitBookResponseError, match="Unsafe"):
        sync(gitbook_api)
    assert len(gitbook_api.requests) == 1


def test_pagination_cycle(gitbook_api):
    gitbook_api.data["/v1/orgs"] = {"items": [], "next": {"page": "same"}}
    with pytest.raises(GitBookResponseError, match="did not advance"):
        sync(gitbook_api)


def test_404_skip_with_warning(gitbook_api, monkeypatch):
    from unittest.mock import Mock

    warning = Mock()
    monkeypatch.setattr("cognee_community_connector_gitbook.gitbook.logger.warning", warning)
    gitbook_api.failures[CONTENT + "/page/child"] = [404]
    assert CHILD not in {r["id"] for r in sync(gitbook_api)}
    warning.assert_called_once()
    assert "404" in warning.call_args.args[0]


@pytest.mark.parametrize("statuses", [[429], [429, 429]])
def test_retry_once(statuses, gitbook_api, monkeypatch):
    sleeps = []
    monkeypatch.setattr("cognee_community_connector_gitbook.gitbook.time.sleep", sleeps.append)
    gitbook_api.failures["/v1/orgs"] = statuses.copy()
    if len(statuses) == 2:
        with pytest.raises(GitBookError, match="429"):
            sync(gitbook_api)
    else:
        assert len(sync(gitbook_api)) == 4
    assert len([r for r in gitbook_api.requests if r.url.path == "/v1/orgs"]) == 2
    assert sleeps[:3] == [0, 0, 0]


@pytest.mark.parametrize(
    "response",
    [httpx.Response(200, content=b"{broken"), httpx.Response(200, json={"items": "wrong"})],
)
def test_malformed_response_aborts(response, gitbook_api, pipeline_factory):
    pipeline = pipeline_factory()
    pipeline.run(source(gitbook_api))
    before = read_rows(pipeline)
    gitbook_api.responses[SITES] = response
    with pytest.raises(GitBookResponseError):
        sync(gitbook_api)
    with pytest.raises(PipelineStepFailed) as caught:
        pipeline.run(source(gitbook_api))
    cause = caught.value
    while cause is not None and not isinstance(cause, GitBookResponseError):
        cause = cause.__cause__
    assert isinstance(cause, GitBookResponseError)
    assert read_rows(pipeline) == before


def test_full_snapshot_updates(gitbook_api, pipeline_factory):
    pipeline = pipeline_factory()
    pipeline.run(source(gitbook_api))
    gitbook_api.data[CONTENT + "/page/page"]["markdown"] = "Edited"
    pipeline = pipeline_factory()
    pipeline.run(source(gitbook_api))
    assert read_rows(pipeline)[PAGE]["content"].endswith("Edited")


@pytest.mark.parametrize("deleted", ["page", "space", "site", "all"])
def test_forget_on_delete(deleted, gitbook_api, pipeline_factory, monkeypatch):
    pipeline = pipeline_factory()
    pipeline.run(source(gitbook_api))
    before = read_rows(pipeline)
    if deleted == "page":
        gitbook_api.data[CONTENT]["pages"][0]["pages"] = []
    elif deleted == "space":
        gitbook_api.data[SPACES]["items"] = []
    elif deleted == "site":
        gitbook_api.data[SITES]["items"] = []
    else:
        gitbook_api.data["/v1/orgs"]["items"] = []
    pipeline = pipeline_factory()
    pipeline.run(source(gitbook_api))
    after = read_rows(pipeline)
    assert CHILD not in after
    assert "gitbook:source" in after
    if deleted in {"site", "all"}:
        assert set(after) == {"gitbook:source"}
    resolver = importlib.import_module("cognee.tasks.ingestion.resolve_dlt_sources")

    def row_data(row):
        return SimpleNamespace(
            table_name="gitbook_documents",
            primary_key_value=row["id"],
            row_data=row,
            content_hash=json.dumps(row, sort_keys=True),
        )

    def identifier(row):
        return uuid5(NAMESPACE_OID, resolver._dlt_row_identifier(row_data(row)))

    monkeypatch.setattr(
        resolver, "ingest_dlt_source", AsyncMock(return_value=[row_data(r) for r in after.values()])
    )
    monkeypatch.setattr(
        resolver,
        "get_unique_data_id",
        AsyncMock(side_effect=lambda key, user: uuid5(NAMESPACE_OID, key)),
    )
    methods = importlib.import_module("cognee.modules.data.methods")
    monkeypatch.setattr(
        methods,
        "get_authorized_existing_datasets",
        AsyncMock(return_value=[SimpleNamespace(id="dataset")]),
    )
    get_data = importlib.import_module("cognee.modules.data.methods.get_dataset_data")
    monkeypatch.setattr(
        get_data,
        "get_dataset_data",
        AsyncMock(
            return_value=[
                SimpleNamespace(id=identifier(r), external_metadata={"source": "gitbook"})
                for r in before.values()
            ]
        ),
    )
    graph = importlib.import_module("cognee.modules.graph.methods.delete_data_nodes_and_edges")
    delete = importlib.import_module("cognee.modules.data.methods.delete_data")
    purge_graph, purge_data = AsyncMock(), AsyncMock()
    monkeypatch.setattr(graph, "delete_data_nodes_and_edges", purge_graph)
    monkeypatch.setattr(delete, "delete_data", purge_data)

    async def resolve_and_clean():
        items, cleanup = await resolver.resolve_dlt_sources(
            source(gitbook_api), "gitbook", SimpleNamespace(id="user")
        )
        assert all(item.external_metadata["source"] == "gitbook" for item in items)
        assert cleanup is not None
        await cleanup()

    asyncio.run(resolve_and_clean())
    removed = set(before) - set(after)
    assert purge_graph.await_count == purge_data.await_count == len(removed)
    for key in removed:
        purge_graph.assert_any_await("dataset", identifier(before[key]), "user")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"api_token": None},
        {"base_url": "invalid"},
        {"base_url": BASE_URL + "?api_token=secret"},
        {"base_url": "https://user:secret@example.test"},
    ],
)
def test_invalid_configuration(kwargs):
    with pytest.raises(ValueError):
        gitbook_source(**({"api_token": "test-token"} | kwargs))


def test_multiple_populated_spaces(gitbook_api):
    page = {"id": "page", "type": "document", "title": "API", "path": "api", "pages": []}
    gitbook_api.data["/v1/spaces/other/content"] = {"pages": [page]}
    gitbook_api.data["/v1/spaces/other/content/page/page"] = {**page, "markdown": "API body"}
    rows = {r["id"]: r for r in sync(gitbook_api)}
    assert "Space: Reference" in rows["page:org:site:other:page"]["content"]
    assert "Space: Manual" in rows[PAGE]["content"]


def test_group_and_empty_document(gitbook_api):
    page = gitbook_api.data[CONTENT]["pages"][0]
    gitbook_api.data[CONTENT]["pages"] = [
        {"id": "group", "type": "group", "title": "Section", "path": "section", "pages": [page]}
    ]
    del gitbook_api.data[CONTENT + "/page/page"]["markdown"]
    rows = {r["id"]: r for r in sync(gitbook_api)}
    assert "page:org:site:space:group" in rows
    assert "Parent page: group" in rows[PAGE]["content"]
    assert rows[PAGE]["content"].endswith("Organization: org\n\n")


@pytest.mark.parametrize("broken", ["markdown", "pages"])
def test_malformed_late_response_preserves_snapshot(broken, gitbook_api, pipeline_factory):
    pipeline = pipeline_factory()
    pipeline.run(source(gitbook_api))
    before = read_rows(pipeline)
    if broken == "markdown":
        gitbook_api.data[CONTENT + "/page/child"]["markdown"] = {}
    else:
        del gitbook_api.data[CONTENT]["pages"][0]["pages"]
    with pytest.raises(GitBookResponseError):
        sync(gitbook_api)
    with pytest.raises(PipelineStepFailed):
        pipeline.run(source(gitbook_api))
    assert read_rows(pipeline) == before


@pytest.mark.parametrize("retry_after", ["3", "Thu, 08 Oct 2026 00:00:03 GMT"])
def test_retry_after_delay(retry_after, monkeypatch):
    from datetime import UTC, datetime

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 8, tzinfo=UTC)

    sleeps, requests = [], []
    monkeypatch.setattr("cognee_community_connector_gitbook.gitbook.datetime", Clock)
    monkeypatch.setattr("cognee_community_connector_gitbook.gitbook.time.sleep", sleeps.append)

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"Retry-After": retry_after})
        return httpx.Response(200, json={"items": []})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert list(_API(client, BASE_URL, "test-token").listing("/v1/orgs")) == []
    assert sleeps == [0, 3, 0]
