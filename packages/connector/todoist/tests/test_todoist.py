"""Focused tests for Todoist mapping, incremental state, and deletions."""

import json
from io import BytesIO
from urllib.error import HTTPError
from urllib.parse import parse_qs

import pytest

from cognee_community_connector_todoist.todoist import (
    _due_text,
    _post_sync,
    _record_to_row,
    _response_rows,
    todoist_source,
)


def test_post_sync_uses_bearer_auth_and_form_encoded_sync_request(monkeypatch):
    observed = {}

    class Response(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    def fake_urlopen(request, timeout):
        observed["url"] = request.full_url
        observed["method"] = request.method
        observed["headers"] = request.headers
        observed["body"] = parse_qs(request.data.decode())
        observed["timeout"] = timeout
        return Response(json.dumps({"sync_token": "next"}).encode())

    monkeypatch.setattr("cognee_community_connector_todoist.todoist.urlopen", fake_urlopen)
    response = _post_sync("secret", "prior", ["projects", "items"])

    assert response["sync_token"] == "next"
    assert observed["url"] == "https://api.todoist.com/api/v1/sync"
    assert observed["method"] == "POST"
    assert observed["headers"]["Authorization"] == "Bearer secret"
    assert observed["headers"]["Content-type"] == "application/x-www-form-urlencoded"
    assert observed["body"] == {
        "sync_token": ["prior"],
        "resource_types": ['["projects", "items"]'],
    }
    assert observed["timeout"] == 30


def test_post_sync_rejects_response_without_cursor(monkeypatch):
    class Response(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    monkeypatch.setattr(
        "cognee_community_connector_todoist.todoist.urlopen",
        lambda *args, **kwargs: Response(b"{}"),
    )
    with pytest.raises(RuntimeError, match="sync_token"):
        _post_sync("secret", "*", ["items"])


def test_post_sync_surfaces_api_status_without_echoing_token(monkeypatch):
    error_body = BytesIO(b'{"error":"Unauthorized","error_tag":"AUTH_INVALID"}')
    api_error = HTTPError(
        "https://api.todoist.com/api/v1/sync", 401, "Unauthorized", {}, error_body
    )

    def fake_urlopen(*args, **kwargs):
        raise api_error

    monkeypatch.setattr("cognee_community_connector_todoist.todoist.urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="HTTP 401: Unauthorized") as raised:
        _post_sync("secret-token", "*", ["items"])
    assert "secret-token" not in str(raised.value)


def test_records_map_projects_tasks_comments_and_deletions():
    rows = _response_rows(
        {
            "projects": [
                {
                    "id": "p1",
                    "name": "Work",
                    "description": "Launch",
                    "parent_id": None,
                    "is_archived": True,
                },
                {"id": "p2", "is_deleted": True},
            ],
            "items": [
                {
                    "id": "t1",
                    "content": "Ship release",
                    "project_id": "p1",
                    "checked": True,
                    "labels": ["release"],
                    "due": {"date": "2026-10-05", "string": "tomorrow"},
                }
            ],
            "notes": [
                {"id": "c1", "content": "Ready", "item_id": "t1", "posted_at": "today"},
            ],
            "project_notes": [
                {"id": "c3", "content": "Kickoff", "project_id": "p1"},
                {"id": "c2", "is_deleted": True},
            ],
        },
        ["projects", "tasks", "comments"],
    )

    assert rows == [
        {
            "id": "project:p1",
            "type": "project",
            "todoist_id": "p1",
            "name": "Work",
            "description": "Launch",
            "parent_id": None,
            "is_archived": True,
            "_deleted": False,
        },
        {"id": "project:p2", "_deleted": True},
        {
            "id": "task:t1",
            "type": "task",
            "todoist_id": "t1",
            "content": "Ship release",
            "description": "",
            "project_id": "p1",
            "section_id": None,
            "parent_id": None,
            "checked": True,
            "due": "tomorrow",
            "labels": "release",
            "priority": None,
            "_deleted": False,
        },
        {
            "id": "comment:c1",
            "type": "comment",
            "todoist_id": "c1",
            "content": "Ready",
            "task_id": "t1",
            "project_id": None,
            "posted_at": "today",
            "_deleted": False,
        },
        {
            "id": "comment:c3",
            "type": "comment",
            "todoist_id": "c3",
            "content": "Kickoff",
            "task_id": None,
            "project_id": "p1",
            "posted_at": None,
            "_deleted": False,
        },
        {"id": "comment:c2", "_deleted": True},
    ]


def test_mapping_requires_stable_identifier():
    with pytest.raises(ValueError, match="valid id"):
        _record_to_row("items", {"content": "No id"})


def test_due_date_mapping_uses_human_text_then_date():
    assert _due_text({"date": "2026-10-05", "string": "tomorrow"}) == "tomorrow"
    assert _due_text({"date": "2026-10-05"}) == "2026-10-05"
    assert _due_text(None) == ""


def test_source_requires_token_and_at_least_one_resource(monkeypatch):
    pytest.importorskip("dlt")
    monkeypatch.delenv("TODOIST_API_TOKEN", raising=False)
    with pytest.raises(ValueError, match="API token"):
        todoist_source(token="")
    with pytest.raises(ValueError, match="at least one"):
        todoist_source(
            token="test", include_projects=False, include_tasks=False, include_comments=False
        )


def test_source_resource_uses_merge_and_hard_delete():
    pytest.importorskip("dlt")
    resource = todoist_source(token="test")
    schema = resource.compute_table_schema()

    assert resource.name == "todoist_records"
    assert schema["columns"]["id"].get("primary_key") is True
    assert schema["columns"]["_deleted"].get("hard_delete") is True
    disposition = schema["write_disposition"]
    if isinstance(disposition, dict):
        disposition = disposition.get("disposition")
    assert disposition == "merge"


def test_dlt_persists_cursor_and_applies_deletion_on_next_sync(tmp_path, monkeypatch):
    dlt = pytest.importorskip("dlt")
    responses = [
        {
            "sync_token": "cursor-1",
            "projects": [{"id": "p1", "name": "Work", "description": ""}],
            "items": [{"id": "t1", "content": "Ship", "project_id": "p1"}],
            "notes": [],
            "project_notes": [],
        },
        {
            "sync_token": "cursor-2",
            "projects": [{"id": "p1", "name": "Work renamed", "description": ""}],
            "items": [{"id": "t1", "is_deleted": True}],
            "notes": [],
            "project_notes": [],
        },
    ]
    requests = []

    def fake_sync(token, sync_token, resource_types):
        requests.append((sync_token, resource_types))
        return responses.pop(0)

    monkeypatch.setattr("cognee_community_connector_todoist.todoist._post_sync", fake_sync)
    db_path = tmp_path / "todoist.db"
    pipeline_dir = str(tmp_path / "state")

    def run_sync():
        pipeline = dlt.pipeline(
            pipeline_name="todoist_cursor_test",
            destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
            dataset_name="todoist",
            pipelines_dir=pipeline_dir,
        )
        pipeline.run(todoist_source(token="test"))
        with pipeline.sql_client() as client:
            return client.execute_sql("SELECT id, name FROM todoist_records ORDER BY id")

    assert [row[0] for row in run_sync()] == ["project:p1", "task:t1"]
    updated_rows = run_sync()
    assert [row[0] for row in updated_rows] == ["project:p1"]
    assert updated_rows[0][1] == "Work renamed"
    assert requests == [
        ("*", ["projects", "items", "notes"]),
        ("cursor-1", ["projects", "items", "notes"]),
    ]


def test_source_can_select_resource_types(tmp_path, monkeypatch):
    dlt = pytest.importorskip("dlt")
    observed = []

    def fake_sync(token, sync_token, resource_types):
        observed.append(resource_types)
        return {
            "sync_token": "cursor-1",
            "projects": [{"id": "p1", "name": "Work"}],
            "items": [{"id": "t1", "content": "Ship"}],
            "notes": [],
            "project_notes": [],
        }

    monkeypatch.setattr("cognee_community_connector_todoist.todoist._post_sync", fake_sync)
    pipeline = dlt.pipeline(
        pipeline_name="todoist_selected_types_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'todoist.db'}"),
        dataset_name="todoist",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(todoist_source(token="test", include_projects=False, include_comments=False))

    with pipeline.sql_client() as client:
        rows = client.execute_sql("SELECT id FROM todoist_records")
    assert [row[0] for row in rows] == ["task:t1"]
    assert observed == [["items"]]


def test_source_rejects_resource_selection_change_with_saved_cursor(tmp_path, monkeypatch):
    dlt = pytest.importorskip("dlt")
    requests = []

    def fake_sync(token, sync_token, resource_types):
        requests.append((sync_token, resource_types))
        return {
            "sync_token": "cursor-1",
            "projects": [{"id": "p1", "name": "Work"}],
            "items": [{"id": "t1", "content": "Ship"}],
            "notes": [],
            "project_notes": [],
        }

    monkeypatch.setattr("cognee_community_connector_todoist.todoist._post_sync", fake_sync)
    db_path = tmp_path / "todoist.db"
    pipeline_dir = str(tmp_path / "state")

    def run_sync(include_projects):
        pipeline = dlt.pipeline(
            pipeline_name="todoist_selection_change_test",
            destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
            dataset_name="todoist",
            pipelines_dir=pipeline_dir,
        )
        return pipeline.run(
            todoist_source(
                token="test",
                include_projects=include_projects,
                include_tasks=True,
                include_comments=False,
            )
        )

    run_sync(include_projects=False)
    with pytest.raises(Exception, match="resource selection changed"):
        run_sync(include_projects=True)
    run_sync(include_projects=False)

    assert requests == [
        ("*", ["items"]),
        ("cursor-1", ["items"]),
    ]


def test_failed_sync_does_not_advance_persisted_cursor(tmp_path, monkeypatch):
    dlt = pytest.importorskip("dlt")
    responses = [
        {"sync_token": "cursor-1", "projects": [], "items": [], "notes": []},
        RuntimeError("temporary API failure"),
        {"sync_token": "cursor-2", "projects": [], "items": [], "notes": []},
    ]
    requested_cursors = []

    def fake_sync(token, sync_token, resource_types):
        requested_cursors.append(sync_token)
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr("cognee_community_connector_todoist.todoist._post_sync", fake_sync)
    db_path = tmp_path / "todoist.db"
    pipeline_dir = str(tmp_path / "state")

    def run_sync():
        pipeline = dlt.pipeline(
            pipeline_name="todoist_failed_cursor_test",
            destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
            dataset_name="todoist",
            pipelines_dir=pipeline_dir,
        )
        return pipeline.run(todoist_source(token="test"))

    run_sync()
    with pytest.raises(Exception, match="temporary API failure"):
        run_sync()
    run_sync()

    assert requested_cursors == ["*", "cursor-1", "cursor-1"]
