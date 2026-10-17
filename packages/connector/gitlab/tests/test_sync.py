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
