from unittest.mock import patch

import httpx
import pytest

from cognee_community_connector_gitlab.gitlab import (
    GitLabAPIError,
    _discover_projects,
    _fetch_issues,
    _fetch_merge_requests,
    _fetch_wiki_pages,
    _validate_token,
    gitlab_source,
)


def token_metadata(**updates):
    return {"active": True, "revoked": False, "expires_at": None, "scopes": ["read_api"], **updates}


def client_for(handler):
    return httpx.Client(
        base_url="https://gitlab.example/prefix/api/v4/",
        headers={"PRIVATE-TOKEN": "test-secret"},
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.parametrize("scope", ["read_api", "api"])
def test_valid_token(scope):
    def handler(request):
        assert request.url.path == "/prefix/api/v4/personal_access_tokens/self"
        assert request.headers["PRIVATE-TOKEN"] == "test-secret"
        return httpx.Response(200, json=token_metadata(scopes=[scope]))

    with client_for(handler) as client:
        assert _validate_token(client)["active"] is True


@pytest.mark.parametrize(
    "updates",
    [
        {"active": False},
        {"active": False, "revoked": True},
        {"active": False, "expires_at": "2000-01-01"},
        {"active": None},
        {"scopes": ["read_user"]},
        {"scopes": []},
        {"scopes": [{}]},
    ],
)
def test_unusable_token(updates):
    with (
        client_for(lambda _: httpx.Response(200, json=token_metadata(**updates))) as client,
        pytest.raises(GitLabAPIError),
    ):
        _validate_token(client)


def test_validation_requires_only_active_status_and_scopes():
    metadata = {"active": True, "scopes": ["read_api"]}
    with client_for(lambda _: httpx.Response(200, json=metadata)) as client:
        assert _validate_token(client) == metadata


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 302])
def test_http_failures_do_not_expose_response_body(status):
    with client_for(lambda _: httpx.Response(status, text="test-secret")) as client:
        with pytest.raises(GitLabAPIError) as exc:
            _validate_token(client)
        assert "test-secret" not in str(exc.value)


def test_project_pagination():
    def handler(request):
        assert request.url.path == "/prefix/api/v4/projects"
        assert request.url.params["pagination"] == "keyset"
        if "id_after" not in request.url.params:
            return httpx.Response(200, json=[{"id": i} for i in range(1, 101)])
        assert request.url.params["id_after"] == "100"
        return httpx.Response(200, json=[{"id": 105}])

    with client_for(handler) as client:
        projects = _discover_projects(client)
    assert len(projects) == 101
    assert projects[-1]["id"] == 105


def test_empty_projects_is_successful():
    with client_for(lambda _: httpx.Response(200, json=[])) as client:
        assert _discover_projects(client) == []


def test_failed_project_page_aborts():
    def handler(request):
        if "id_after" not in request.url.params:
            return httpx.Response(200, json=[{"id": i} for i in range(1, 101)])
        return httpx.Response(403)

    with client_for(handler) as client, pytest.raises(GitLabAPIError):
        _discover_projects(client)


@pytest.mark.parametrize("payload", [{}, [{"id": 1}, {"id": 1}], [{"name": "no-id"}]])
def test_invalid_projects_abort(payload):
    with (
        client_for(lambda _: httpx.Response(200, json=payload)) as client,
        pytest.raises(GitLabAPIError),
    ):
        _discover_projects(client)


def test_timeout():
    def handler(request):
        raise httpx.ReadTimeout("test-secret", request=request)

    with client_for(handler) as client:
        with pytest.raises(GitLabAPIError, match="network error or timeout") as exc:
            _validate_token(client)
        assert "test-secret" not in str(exc.value)


def test_source_validates_before_discovery():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/self"):
            return httpx.Response(200, json=token_metadata())
        return httpx.Response(200, json=[])

    with (
        patch(
            "cognee_community_connector_gitlab.gitlab.httpx.Client",
            return_value=client_for(handler),
        ),
        pytest.raises(NotImplementedError),
    ):
        gitlab_source("test-secret", resource_types=["issues"])
    assert paths == ["/prefix/api/v4/personal_access_tokens/self", "/prefix/api/v4/projects"]


def test_empty_selection_rejected_before_network():
    with pytest.raises(ValueError, match="at least one"):
        gitlab_source("test-secret", resource_types=[])


def test_invalid_json():
    with (
        client_for(lambda _: httpx.Response(200, text="not-json")) as client,
        pytest.raises(GitLabAPIError, match="invalid JSON"),
    ):
        _validate_token(client)


def test_invalid_token_stops_discovery():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(401)

    with (
        patch(
            "cognee_community_connector_gitlab.gitlab.httpx.Client",
            return_value=client_for(handler),
        ),
        pytest.raises(GitLabAPIError),
    ):
        gitlab_source("test-secret", resource_types=["issues"])
    assert paths == ["/prefix/api/v4/personal_access_tokens/self"]


def issue_payload(issue_id, project_id=42):
    return {"id": issue_id, "iid": issue_id, "project_id": project_id, "title": "Example"}


def test_issue_pagination_preserves_open_and_closed_payloads():
    first_page = [issue_payload(i) for i in range(1, 101)]
    first_page[0]["state"] = "opened"
    final_page = [{**issue_payload(101), "state": "closed", "description": None}]

    def handler(request):
        assert request.url.path == "/prefix/api/v4/projects/42/issues"
        assert request.headers["PRIVATE-TOKEN"] == "test-secret"
        assert request.url.params["scope"] == "all"
        assert request.url.params["state"] == "all"
        page = request.url.params["page"]
        assert page in ("1", "2")
        return httpx.Response(200, json=first_page if page == "1" else final_page)

    with client_for(handler) as client:
        assert _fetch_issues(client, 42) == first_page + final_page


def test_empty_issue_list():
    with client_for(lambda _: httpx.Response(200, json=[])) as client:
        assert _fetch_issues(client, 42) == []


def test_failed_issue_page_aborts():
    def handler(request):
        if request.url.params["page"] == "1":
            return httpx.Response(200, json=[issue_payload(i) for i in range(1, 101)])
        return httpx.Response(403)

    with client_for(handler) as client, pytest.raises(GitLabAPIError):
        _fetch_issues(client, 42)


@pytest.mark.parametrize(
    "payload",
    [{}, [None], [{"id": 1}], [issue_payload(1, project_id=99)], [issue_payload(1)] * 2],
)
def test_invalid_issue_inventory_aborts(payload):
    with (
        client_for(lambda _: httpx.Response(200, json=payload)) as client,
        pytest.raises(GitLabAPIError),
    ):
        _fetch_issues(client, 42)


@pytest.mark.parametrize("selection", [["invalid"], "issues", [None]])
def test_invalid_selection_rejected_before_network(selection):
    with (
        patch("cognee_community_connector_gitlab.gitlab.httpx.Client") as client,
        pytest.raises(ValueError, match="Resource types"),
    ):
        gitlab_source("test-secret", resource_types=selection)
    client.assert_not_called()


@pytest.mark.parametrize(
    "selection", [["issues"], ["merge_requests"], ["wiki"], ["issues", "merge_requests", "wiki"]]
)
def test_source_fetches_only_selected_resources(selection):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/self"):
            return httpx.Response(200, json=token_metadata())
        if request.url.path.endswith("/projects"):
            return httpx.Response(200, json=[{"id": 42}, {"id": 105}])
        return httpx.Response(200, json=[])

    with (
        patch(
            "cognee_community_connector_gitlab.gitlab.httpx.Client",
            return_value=client_for(handler),
        ),
        pytest.raises(NotImplementedError),
    ):
        gitlab_source("test-secret", resource_types=selection)
    expected = ["/prefix/api/v4/personal_access_tokens/self", "/prefix/api/v4/projects"]
    for project_id in (42, 105):
        for resource, endpoint in (
            ("issues", "issues"),
            ("merge_requests", "merge_requests"),
            ("wiki", "wikis"),
        ):
            if resource in selection:
                expected.append(f"/prefix/api/v4/projects/{project_id}/{endpoint}")
    assert paths == expected


def test_merge_request_pagination_preserves_all_states():
    first_page = [issue_payload(i) for i in range(1, 101)]
    for item, state in zip(first_page, ("opened", "closed", "merged", "locked"), strict=False):
        item["state"] = state
    final_page = [issue_payload(101)]

    def handler(request):
        assert request.url.path == "/prefix/api/v4/projects/42/merge_requests"
        assert request.url.params["state"] == "all"
        assert request.url.params["scope"] == "all"
        page = request.url.params["page"]
        assert page in ("1", "2")
        return httpx.Response(200, json=first_page if page == "1" else final_page)

    with client_for(handler) as client:
        assert _fetch_merge_requests(client, 42) == first_page + final_page


def wiki_payload(slug="guides/setup"):
    return {"slug": slug, "title": "Setup", "content": "# Instructions", "format": "markdown"}


def test_wiki_returns_entire_inventory_with_content():
    pages = [wiki_payload(f"guides/page-{i}") for i in range(150)]
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.path == "/prefix/api/v4/projects/42/wikis"
        assert dict(request.url.params) == {"with_content": "1"}
        return httpx.Response(200, json=pages)

    with client_for(handler) as client:
        assert _fetch_wiki_pages(client, 42) == pages
    assert len(calls) == 1


@pytest.mark.parametrize("fetch", [_fetch_merge_requests, _fetch_wiki_pages])
def test_empty_resource(fetch):
    with client_for(lambda _: httpx.Response(200, json=[])) as client:
        assert fetch(client, 42) == []


@pytest.mark.parametrize("status", [403, 404, 500])
@pytest.mark.parametrize("fetch", [_fetch_merge_requests, _fetch_wiki_pages])
def test_resource_access_failure_is_not_empty(fetch, status):
    with (
        client_for(lambda _: httpx.Response(status)) as client,
        pytest.raises(GitLabAPIError),
    ):
        fetch(client, 42)


@pytest.mark.parametrize(
    "payload", [{}, [None], [{"slug": "home"}], [wiki_payload("")], [wiki_payload()] * 2]
)
def test_invalid_wiki_payload(payload):
    with (
        client_for(lambda _: httpx.Response(200, json=payload)) as client,
        pytest.raises(GitLabAPIError),
    ):
        _fetch_wiki_pages(client, 42)


def test_failed_merge_request_page_aborts():
    def handler(request):
        if request.url.params["page"] == "1":
            return httpx.Response(200, json=[issue_payload(i) for i in range(1, 101)])
        return httpx.Response(500)

    with client_for(handler) as client, pytest.raises(GitLabAPIError):
        _fetch_merge_requests(client, 42)


@pytest.mark.parametrize("payload", [{}, [issue_payload(1, 99)], [issue_payload(1)] * 2])
def test_invalid_merge_request_payload(payload):
    with (
        client_for(lambda _: httpx.Response(200, json=payload)) as client,
        pytest.raises(GitLabAPIError),
    ):
        _fetch_merge_requests(client, 42)
