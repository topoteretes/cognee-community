from copy import deepcopy
from unittest.mock import patch

import httpx
import pytest
from cognee_community_connector_gitlab.gitlab import GitLabAPIError
from cognee_community_connector_gitlab.sync import sync_documents


def item(identity, timestamp="2026-10-17T10:00:00Z", **changes):
    return {
        "id": identity,
        "iid": identity,
        "project_id": 42,
        "title": f"Issue {identity}",
        "updated_at": timestamp,
        **changes,
    }


def run_sync(
    state,
    inventory,
    *,
    delta=None,
    wiki=None,
    projects=None,
    project_status=404,
    project_payload=None,
):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/projects"):
            payload = (
                projects
                if projects is not None
                else [{"id": 42, "path_with_namespace": "team/app"}]
            )
        elif request.url.path.endswith("/projects/42"):
            return httpx.Response(project_status, json=project_payload)
        elif "/issues/" in request.url.path:
            identity = int(request.url.path.rsplit("/", 1)[1])
            match = next((item for item in inventory if item["iid"] == identity), None)
            return httpx.Response(200, json=match) if match else httpx.Response(404)
        elif "/wikis/" in request.url.path:
            return httpx.Response(404)
        elif request.url.path.endswith("/wikis"):
            payload = wiki or []
        else:
            payload = delta if "updated_after" in request.url.params else inventory
            if isinstance(payload, int):
                return httpx.Response(payload)
        return httpx.Response(200, json=payload)

    with httpx.Client(
        base_url="https://gitlab.example/api/v4/", transport=httpx.MockTransport(handler)
    ) as client:
        rows = list(sync_documents(client, "https://gitlab.example", ("issues", "wiki"), state))
    return rows, requests


def test_initial_then_delta_hashes_and_deletions():
    state = {}
    wiki = [{"slug": "home", "title": "Home", "content": "Welcome"}]
    first, _ = run_sync(state, [item(1), item(2)], wiki=wiki)
    assert len(first) == 3
    assert state["projects"]["42"]["issues"]["updated_after"] == "2026-10-17T10:00:00+00:00"
    changed = item(2, "2026-10-17T11:00:00Z", description="Edited")
    second, requests = run_sync(state, [changed, item(3)], delta=[changed, item(3)], wiki=wiki)
    assert len(second) == 3
    assert {row["id"] for row in second if row.get("_deleted")} == {first[0]["id"]}
    issue_requests = [r for r in requests if r.url.path.endswith("/issues")]
    assert issue_requests[0].url.params["updated_after"] == "2026-10-17T09:59:00+00:00"
    assert "updated_after" not in issue_requests[1].url.params
    third, _ = run_sync(state, [changed, item(3)], delta=[], wiki=wiki)
    assert third == []


def test_empty_delta_does_not_delete_unchanged_records():
    state = {}
    run_sync(state, [item(1)])
    rows, _ = run_sync(state, [item(1)], delta=[])
    assert rows == []
    assert len(state["projects"]["42"]["issues"]["hashes"]) == 1


def test_wiki_edit_and_removal():
    state = {}
    first, _ = run_sync(state, [], wiki=[{"slug": "home", "title": "Home", "content": "A"}])
    rows, _ = run_sync(state, [], wiki=[{"slug": "home", "title": "Home", "content": "B"}])
    assert len(rows) == 1 and "B" in rows[0]["content"]
    rows, _ = run_sync(state, [])
    assert rows == [{"id": first[0]["id"], "_deleted": True}]


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
def test_failed_inventory_preserves_state(status):
    state = {}
    run_sync(state, [item(1)])
    before = deepcopy(state)
    with pytest.raises(GitLabAPIError):
        run_sync(state, status, delta=[item(2)])
    assert state == before


def test_missing_project_deletes_all_known_documents():
    state = {}
    wiki = [{"slug": "home", "title": "Home", "content": "Welcome"}]
    first, _ = run_sync(state, [item(1), item(2)], wiki=wiki)
    rows, requests = run_sync(state, [], projects=[])
    assert {row["id"] for row in rows} == {row["id"] for row in first}
    assert all(row["_deleted"] is True for row in rows)
    assert state == {"projects": {}}
    assert requests[-1].url.path.endswith("/projects/42")


def test_project_skipped_by_discovery_is_still_synced():
    state = {}
    run_sync(state, [item(1)])
    before = deepcopy(state)
    rows, _ = run_sync(
        state,
        [item(1)],
        delta=[],
        projects=[],
        project_status=200,
        project_payload={"id": 42, "path_with_namespace": "team/app"},
    )
    assert rows == [] and state == before


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_missing_project_read_failure_preserves_state(status):
    state = {}
    run_sync(state, [item(1)])
    before = deepcopy(state)
    with pytest.raises(GitLabAPIError):
        run_sync(state, [], projects=[], project_status=status)
    assert state == before


@pytest.mark.parametrize("payload", [[], {}, {"id": 99}, {"id": "42"}])
def test_invalid_project_read_preserves_state(payload):
    state = {}
    run_sync(state, [item(1)])
    before = deepcopy(state)
    with pytest.raises(GitLabAPIError, match="project identity"):
        run_sync(state, [], projects=[], project_status=200, project_payload=payload)
    assert state == before


@pytest.mark.parametrize("timestamp", [None, "bad", "2026-10-17T10:00:00"])
def test_invalid_timestamp_cannot_advance_checkpoint(timestamp):
    state = {}
    with pytest.raises(GitLabAPIError, match="updated_at"):
        run_sync(state, [item(1, timestamp)])
    assert state == {}


def test_same_timestamp_and_edit_during_inventory_are_not_lost():
    state = {}
    run_sync(state, [item(1)])
    # A same-timestamp new item and a later edit missing from the earlier delta
    # are both captured by the complete inventory.
    changed = item(1, "2026-10-17T11:00:00Z", description="Concurrent edit")
    rows, _ = run_sync(state, [changed, item(2)], delta=[])
    assert len(rows) == 2


def test_state_not_published_when_generator_is_closed_early():
    state = {}
    with (
        patch(
            "cognee_community_connector_gitlab.gitlab._discover_projects",
            return_value=[{"id": 42, "path_with_namespace": "team/app"}],
        ),
        patch("cognee_community_connector_gitlab.gitlab._fetch_issues", return_value=[item(1)]),
    ):
        generator = sync_documents(None, "https://gitlab.example", ("issues",), state)
        next(generator)
        generator.close()
    assert state == {}


def test_live_record_skipped_by_listing_is_not_deleted():
    state = {}
    run_sync(state, [item(1)])
    before = deepcopy(state)
    with (
        patch(
            "cognee_community_connector_gitlab.gitlab._discover_projects",
            return_value=[{"id": 42, "path_with_namespace": "team/app"}],
        ),
        patch("cognee_community_connector_gitlab.gitlab._fetch_issues", return_value=[]),
        patch("cognee_community_connector_gitlab.gitlab._get_json", return_value=item(1)) as get,
    ):
        rows = list(sync_documents(None, "https://gitlab.example", ("issues",), state))
    assert rows == [] and state == before
    assert get.call_args.args[1] == "projects/42/issues/1"


def test_later_resource_failure_does_not_publish_earlier_changes():
    state = {}
    run_sync(state, [item(1)])
    before = deepcopy(state)
    with (
        patch(
            "cognee_community_connector_gitlab.gitlab._discover_projects",
            return_value=[{"id": 42, "path_with_namespace": "team/app"}],
        ),
        patch(
            "cognee_community_connector_gitlab.gitlab._fetch_issues",
            return_value=[item(1, description="Changed")],
        ),
        patch(
            "cognee_community_connector_gitlab.gitlab._fetch_wiki_pages",
            side_effect=GitLabAPIError("Wiki fetch failed"),
        ),
    ):
        generator = sync_documents(None, "https://gitlab.example", ("issues", "wiki"), state)
        with pytest.raises(GitLabAPIError):
            next(generator)
    assert state == before


def test_reconciliation_permission_failure_preserves_state():
    state = {}
    run_sync(state, [item(1)])
    before = deepcopy(state)
    with (
        patch(
            "cognee_community_connector_gitlab.gitlab._discover_projects",
            return_value=[{"id": 42, "path_with_namespace": "team/app"}],
        ),
        patch("cognee_community_connector_gitlab.gitlab._fetch_issues", return_value=[]),
        patch(
            "cognee_community_connector_gitlab.gitlab._get_json",
            side_effect=GitLabAPIError("GitLab denied access"),
        ),
        pytest.raises(GitLabAPIError),
    ):
        list(sync_documents(None, "https://gitlab.example", ("issues",), state))
    assert state == before


def run_projects(
    state, projects, responses, *, selected=("issues", "merge_requests", "wiki"), **kw
):
    """Sync several projects.

    ``responses`` maps "<project_id>/<endpoint>" to a payload or status.
    """
    requests = []

    def handler(request):
        requests.append(request)
        path = request.url.path.removeprefix("/api/v4/projects")
        if path == "":
            return httpx.Response(200, json=projects)
        response = responses.get(path.strip("/"), [])
        if isinstance(response, int):
            return httpx.Response(response)
        return httpx.Response(200, json=response)

    with httpx.Client(
        base_url="https://gitlab.example/api/v4/", transport=httpx.MockTransport(handler)
    ) as client:
        rows = list(sync_documents(client, "https://gitlab.example", selected, state, **kw))
    return rows, [request.url.path for request in requests]


def project_item(project_id, identity):
    return {**item(identity), "id": project_id * 1000 + identity, "project_id": project_id}


@pytest.mark.parametrize(
    "flags",
    [
        {"issues_access_level": "disabled", "wiki_access_level": "disabled"},
        {"issues_enabled": False, "wiki_enabled": False},
    ],
)
def test_disabled_features_are_skipped_and_sync_continues(flags):
    projects = [
        {"id": 1, "path_with_namespace": "team/mr-only", **flags},
        {"id": 2, "path_with_namespace": "team/full"},
    ]
    responses = {
        "1/merge_requests": [project_item(1, 1)],
        "2/issues": [project_item(2, 1)],
        "2/merge_requests": [project_item(2, 2)],
        "2/wikis": [{"slug": "home", "title": "Home", "content": "Hi"}],
    }
    rows, paths = run_projects({}, projects, responses)
    assert len(rows) == 4
    assert "/api/v4/projects/1/issues" not in paths
    assert "/api/v4/projects/1/wikis" not in paths
    assert "/api/v4/projects/1/merge_requests" in paths


@pytest.mark.parametrize("status", [403, 404])
def test_unavailable_endpoints_are_skipped_without_metadata(status):
    projects = [{"id": 1, "path_with_namespace": "a/b"}, {"id": 2, "path_with_namespace": "c/d"}]
    responses = {
        "1/issues": status,
        "1/wikis": status,
        "1/merge_requests": [project_item(1, 1)],
        "2/issues": [project_item(2, 1)],
    }
    state = {}
    rows, _ = run_projects(state, projects, responses)
    assert {row["id"].split(":", 2)[2] for row in rows} == {
        "project:1:merge_request:1001",
        "project:2:issue:2001",
    }
    assert set(state["projects"]["1"]) == {"merge_requests"}


@pytest.mark.parametrize("status", [401, 429, 500])
def test_other_resource_failures_still_abort(status):
    projects = [{"id": 1, "path_with_namespace": "a/b"}]
    state = {}
    with pytest.raises(GitLabAPIError):
        run_projects(state, projects, {"1/wikis": status})
    assert state == {}


def test_feature_turned_off_later_removes_its_documents():
    project = {"id": 1, "path_with_namespace": "a/b"}
    responses = {
        "1/issues": [project_item(1, 1)],
        "1/wikis": [{"slug": "home", "title": "Home", "content": "Hi"}],
    }
    state = {}
    first, _ = run_projects(state, [project], responses, selected=("issues", "wiki"))
    wiki_id = next(row["id"] for row in first if ":wiki:" in row["id"])
    rows, paths = run_projects(
        state,
        [{**project, "wiki_access_level": "disabled"}],
        responses,
        selected=("issues", "wiki"),
    )
    assert rows == [{"id": wiki_id, "_deleted": True}]
    assert "wiki" not in state["projects"]["1"]
    assert "/api/v4/projects/1/wikis" not in paths


def test_selected_projects_skip_discovery_and_remove_inaccessible_ones():
    state = {}
    responses = {
        "1": {"id": 1, "path_with_namespace": "a/b"},
        "2": {"id": 2, "path_with_namespace": "c/d"},
        "1/issues": [project_item(1, 1)],
        "2/issues": [project_item(2, 1)],
    }
    first, paths = run_projects(state, [], responses, selected=("issues",), project_ids=(1, 2))
    assert len(first) == 2 and "/api/v4/projects" not in paths
    removed = next(row["id"] for row in first if ":project:2:" in row["id"])
    rows, paths = run_projects(
        state, [], {**responses, "2": 404}, selected=("issues",), project_ids=(1, 2)
    )
    assert rows == [{"id": removed, "_deleted": True}]
    assert set(state["projects"]) == {"1"}
    assert paths.count("/api/v4/projects/2") == 1


def with_notes(identity, updated_at, count, *, project_id=1):
    return {
        **project_item(project_id, identity),
        "updated_at": updated_at,
        "user_notes_count": count,
    }


def comment(note_id, body):
    return {"id": note_id, "body": body, "system": False, "author": {"username": "bob"}}


COMMENT_SELECTION = ("comments", "issues", "merge_requests")
PROJECT_ONE = [{"id": 1, "path_with_namespace": "a/b"}]


def notes_calls(paths):
    return [path for path in paths if path.endswith("/notes")]


def test_comments_follow_updated_at_and_note_count():
    state = {}
    t0, t1 = "2026-10-17T10:00:00Z", "2026-10-17T10:05:00Z"
    responses = {
        "1/issues": [with_notes(1, t0, 1), with_notes(2, t0, 0)],
        "1/merge_requests": [with_notes(3, t0, 1)],
        "1/issues/1/notes": [comment(10, "first")],
        "1/issues/2/notes": [],
        "1/merge_requests/3/notes": [comment(30, "review note")],
    }
    first, paths = run_projects(state, PROJECT_ONE, responses, selected=COMMENT_SELECTION)
    assert len(first) == 3 and len(notes_calls(paths)) == 3
    issue_one = next(row for row in first if row["id"].endswith(":issue:1001"))
    assert "first" in issue_one["content"]
    assert any("review note" in row["content"] for row in first)

    # Unchanged marks: no note reads, nothing emitted.
    rows, paths = run_projects(state, PROJECT_ONE, responses, selected=COMMENT_SELECTION)
    assert rows == [] and notes_calls(paths) == []

    # A comment added: GitLab bumps updated_at.
    responses["1/issues"] = [with_notes(1, t1, 2), with_notes(2, t0, 0)]
    responses["1/issues/1/notes"] = [comment(10, "first"), comment(11, "second")]
    rows, paths = run_projects(state, PROJECT_ONE, responses, selected=COMMENT_SELECTION)
    assert notes_calls(paths) == ["/api/v4/projects/1/issues/1/notes"]
    assert len(rows) == 1 and "second" in rows[0]["content"]

    # A comment edited: updated_at bumps again, count unchanged.
    t2 = "2026-10-17T10:10:00Z"
    responses["1/issues"] = [with_notes(1, t2, 2), with_notes(2, t0, 0)]
    responses["1/issues/1/notes"] = [comment(10, "first, edited"), comment(11, "second")]
    rows, _ = run_projects(state, PROJECT_ONE, responses, selected=COMMENT_SELECTION)
    assert len(rows) == 1 and "first, edited" in rows[0]["content"]

    # A comment deleted: GitLab leaves updated_at alone, only the count drops.
    responses["1/issues"] = [with_notes(1, t2, 1), with_notes(2, t0, 0)]
    responses["1/issues/1/notes"] = [comment(10, "first, edited")]
    rows, paths = run_projects(state, PROJECT_ONE, responses, selected=COMMENT_SELECTION)
    assert notes_calls(paths) == ["/api/v4/projects/1/issues/1/notes"]
    assert len(rows) == 1 and "second" not in rows[0]["content"]
    assert rows[0]["id"] == issue_one["id"]


def test_comment_mode_still_deletes_removed_items():
    state = {}
    t0 = "2026-10-17T10:00:00Z"
    responses = {
        "1/issues": [with_notes(1, t0, 1)],
        "1/issues/1/notes": [comment(10, "first")],
        "1/issues/1": 404,
    }
    first, _ = run_projects(state, PROJECT_ONE, responses, selected=("comments", "issues"))
    responses["1/issues"] = []
    rows, _ = run_projects(state, PROJECT_ONE, responses, selected=("comments", "issues"))
    assert rows == [{"id": first[0]["id"], "_deleted": True}]
    assert state["projects"]["1"]["issues"]["notes_marks"] == {}


def test_notes_failure_preserves_state():
    state = {}
    t0 = "2026-10-17T10:00:00Z"
    responses = {"1/issues": [with_notes(1, t0, 1)], "1/issues/1/notes": [comment(10, "a")]}
    run_projects(state, PROJECT_ONE, responses, selected=("comments", "issues"))
    before = deepcopy(state)
    responses["1/issues"] = [with_notes(1, "2026-10-17T11:00:00Z", 2)]
    responses["1/issues/1/notes"] = 500
    with pytest.raises(GitLabAPIError):
        run_projects(state, PROJECT_ONE, responses, selected=("comments", "issues"))
    assert state == before


def internal_comment(note_id, body):
    return {**comment(note_id, body), "internal": True}


def test_internal_comments_are_left_out_by_default():
    t0 = "2026-10-17T10:00:00Z"
    responses = {
        "1/issues": [with_notes(1, t0, 2)],
        "1/merge_requests": [with_notes(2, t0, 1)],
        "1/issues/1/notes": [comment(10, "public"), internal_comment(11, "secret plan")],
        "1/merge_requests/2/notes": [{**comment(20, "old secret"), "confidential": True}],
    }
    rows, _ = run_projects({}, PROJECT_ONE, responses, selected=COMMENT_SELECTION)
    issue = next(row for row in rows if ":issue:" in row["id"])
    merge_request = next(row for row in rows if ":merge_request:" in row["id"])
    assert "public" in issue["content"]
    assert "secret plan" not in issue["content"] and "[internal]" not in issue["content"]
    assert "## Comments" not in merge_request["content"]


def test_include_internal_keeps_marked_internal_comments():
    responses = {
        "1/issues": [with_notes(1, "2026-10-17T10:00:00Z", 2)],
        "1/issues/1/notes": [comment(10, "public"), internal_comment(11, "secret plan")],
    }
    rows, _ = run_projects(
        {}, PROJECT_ONE, responses, selected=("comments", "issues"), include_internal=True
    )
    assert "bob [internal]:\nsecret plan" in rows[0]["content"]


def test_confidential_issues_are_skipped_and_removed_once_confidential():
    state = {}
    t0, t1 = "2026-10-17T10:00:00Z", "2026-10-17T10:05:00Z"
    responses = {
        "1/issues": [
            with_notes(1, t0, 0),
            {**with_notes(2, t0, 0), "confidential": True},
        ],
    }
    first, _ = run_projects(state, PROJECT_ONE, responses, selected=("issues",))
    assert [row["id"] for row in first] == ["https://gitlab.example:project:1:issue:1001"]

    # Issue 1 turns confidential. It still reads successfully, so the deletion
    # check must not keep it; it is removed like a deleted item.
    responses["1/issues"] = [{**with_notes(1, t1, 0), "confidential": True}]
    responses["1/issues/1"] = {**with_notes(1, t1, 0), "confidential": True}
    rows, _ = run_projects(state, PROJECT_ONE, responses, selected=("issues",))
    assert rows == [{"id": first[0]["id"], "_deleted": True}]
    assert state["projects"]["1"]["issues"]["hashes"] == {}


def test_include_internal_keeps_confidential_issues():
    responses = {"1/issues": [{**with_notes(1, "2026-10-17T10:00:00Z", 0), "confidential": True}]}
    rows, _ = run_projects({}, PROJECT_ONE, responses, selected=("issues",), include_internal=True)
    assert len(rows) == 1


def test_without_comments_no_notes_are_read():
    responses = {"1/issues": [with_notes(1, "2026-10-17T10:00:00Z", 3)]}
    rows, paths = run_projects({}, PROJECT_ONE, responses, selected=("issues", "wiki"))
    assert notes_calls(paths) == []
    assert "## Comments" not in rows[0]["content"]
