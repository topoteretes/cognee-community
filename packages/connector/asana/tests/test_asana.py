"""Tests for the incremental Asana document connector."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from urllib.parse import urlparse
from uuid import NAMESPACE_OID, uuid5

import pytest

from cognee_community_connector_asana.asana import (
    ASANA_SOURCE_NAME,
    ASANA_TABLE_NAME,
    AsanaAPIError,
    AsanaNotFoundError,
    _api_get,
    _make_session,
    _paginate,
    _project_to_row,
    _story_fingerprint,
    _task_to_row,
    asana_source,
    sync_asana,
)


def _project(project_id="p1", modified_at="2026-10-01T09:00:00.000Z"):
    return {
        "gid": project_id,
        "name": "Launch plan",
        "notes": "Ship the new product safely.",
        "archived": False,
        "created_at": "2026-09-01T09:00:00.000Z",
        "modified_at": modified_at,
        "permalink_url": f"https://app.asana.com/0/{project_id}/list",
        "owner": {"name": "Priya"},
        "team": {"name": "Engineering"},
        "workspace": {"name": "Acme"},
    }


def _task(
    task_id,
    name,
    modified_at="2026-10-01T10:00:00.000Z",
    parent_id=None,
):
    return {
        "gid": task_id,
        "name": name,
        "notes": f"Description for {name}",
        "completed": False,
        "created_at": "2026-09-01T10:00:00.000Z",
        "modified_at": modified_at,
        "due_on": "2026-10-31",
        "permalink_url": f"https://app.asana.com/0/p1/{task_id}",
        "assignee": {"name": "Rohan"},
        "parent": {"gid": parent_id, "name": "Parent"} if parent_id else None,
        "memberships": [
            {
                "project": {"gid": "p1", "name": "Launch plan"},
                "section": {"name": "In progress"},
            }
        ],
        "tags": [{"name": "priority"}],
        "custom_fields": [{"name": "Risk", "display_value": "Low"}],
    }


def _comment(story_id, text, created_at="2026-10-01T10:30:00.000Z"):
    return {
        "gid": story_id,
        "type": "comment",
        "resource_subtype": "comment_added",
        "text": text,
        "created_at": created_at,
        "created_by": {"name": "Asha"},
    }


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return deepcopy(self._payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeAsanaClient:
    """Small mutable Asana API used by sync and dlt tests."""

    def __init__(self):
        self.projects = {"p1": _project()}
        self.workspace_projects = {"w1": ["p1"]}
        self.project_tasks = {"p1": ["t1"]}
        self.tasks = {
            "t1": _task("t1", "Prepare launch"),
            "s1": _task("s1", "Write runbook", parent_id="t1"),
        }
        self.subtasks = {"t1": ["s1"], "s1": []}
        self.comments = {"t1": [_comment("c1", "Remember the rollback plan.")], "s1": []}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        path = urlparse(url).path.removeprefix("/api/1.0/")
        params = dict(params or {})
        self.calls.append((path, params, timeout))

        if path.startswith("workspaces/") and path.endswith("/projects"):
            workspace_id = path.split("/")[1]
            data = [self.projects[item] for item in self.workspace_projects[workspace_id]]
            return FakeResponse({"data": data, "next_page": None})

        if path.startswith("projects/") and path.endswith("/tasks"):
            project_id = path.split("/")[1]
            data = [self.tasks[item] for item in self.project_tasks.get(project_id, [])]
            return FakeResponse({"data": data, "next_page": None})

        if path.startswith("projects/"):
            project_id = path.split("/")[1]
            if project_id not in self.projects:
                return FakeResponse({"errors": [{"message": "not found"}]}, status_code=404)
            return FakeResponse({"data": self.projects[project_id]})

        if path == "tasks":
            project_id = params["project"]
            cursor = params["modified_since"]
            data = [
                self.tasks[item]
                for item in self.project_tasks.get(project_id, [])
                if self.tasks[item]["modified_at"] > cursor
            ]
            return FakeResponse({"data": data, "next_page": None})

        if path.startswith("tasks/") and path.endswith("/subtasks"):
            task_id = path.split("/")[1]
            data = [self.tasks[item] for item in self.subtasks.get(task_id, [])]
            return FakeResponse({"data": data, "next_page": None})

        if path.startswith("tasks/") and path.endswith("/stories"):
            task_id = path.split("/")[1]
            return FakeResponse({"data": self.comments.get(task_id, []), "next_page": None})

        if path.startswith("tasks/"):
            task_id = path.split("/")[1]
            return FakeResponse({"data": self.tasks[task_id]})

        raise AssertionError(f"Unexpected request: {path} {params}")


def _rows_by_id(client, state):
    return {row["id"]: row for row in sync_asana(client, state, project_ids=["p1"])}


def test_first_sync_ingests_projects_tasks_subtasks_and_comments():
    client = FakeAsanaClient()
    state = {}

    rows = _rows_by_id(client, state)

    assert set(rows) == {"project:p1", "task:t1", "task:s1"}
    assert rows["task:s1"]["kind"] == "subtask"
    assert "Remember the rollback plan" in rows["task:t1"]["content"]
    assert state["known_ids"] == ["project:p1", "task:s1", "task:t1"]
    assert state["last_modified_at"] == "2026-10-01T10:00:00.000Z"


def test_unchanged_resync_uses_modified_since_and_yields_nothing():
    client = FakeAsanaClient()
    state = {}
    _rows_by_id(client, state)
    client.calls.clear()

    assert _rows_by_id(client, state) == {}

    changed_calls = [(path, params) for path, params, _ in client.calls if path == "tasks"]
    assert len(changed_calls) == 1
    assert changed_calls[0][0] == "tasks"
    assert changed_calls[0][1]["project"] == "p1"
    assert changed_calls[0][1]["modified_since"] == "2026-10-01T10:00:00.000Z"


def test_comment_only_change_reingests_task_without_cursor_change():
    client = FakeAsanaClient()
    state = {}
    _rows_by_id(client, state)

    client.comments["t1"].append(_comment("c2", "The launch is approved."))
    rows = _rows_by_id(client, state)

    assert set(rows) == {"task:t1"}
    assert "The launch is approved" in rows["task:t1"]["content"]
    assert state["last_modified_at"] == "2026-10-01T10:00:00.000Z"


def test_failed_comment_poll_does_not_advance_state():
    client = FakeAsanaClient()
    state = {}
    _rows_by_id(client, state)
    state_before_failure = deepcopy(state)
    original_get = client.get

    def fail_one_story_poll(url, params=None, timeout=None):
        if urlparse(url).path.endswith("/tasks/s1/stories"):
            raise RuntimeError("temporary Asana failure")
        return original_get(url, params=params, timeout=timeout)

    client.get = fail_one_story_poll
    with pytest.raises(RuntimeError, match="temporary Asana failure"):
        _rows_by_id(client, state)

    assert state == state_before_failure


def test_modified_since_change_advances_cursor():
    client = FakeAsanaClient()
    state = {}
    _rows_by_id(client, state)

    client.tasks["t1"]["name"] = "Prepare public launch"
    client.tasks["t1"]["modified_at"] = "2026-10-02T11:00:00.000Z"
    rows = _rows_by_id(client, state)

    assert set(rows) == {"task:t1"}
    assert rows["task:t1"]["title"] == "Prepare public launch"
    assert state["last_modified_at"] == "2026-10-02T11:00:00.000Z"


def test_disappearing_subtask_emits_hard_delete():
    client = FakeAsanaClient()
    state = {}
    _rows_by_id(client, state)

    client.subtasks["t1"] = []
    rows = _rows_by_id(client, state)

    assert rows == {"task:s1": {"id": "task:s1", "_deleted": True}}
    assert "task:s1" not in state["known_ids"]


def test_disappearing_project_forgets_its_documents():
    client = FakeAsanaClient()
    state = {}
    _rows_by_id(client, state)

    client.projects = {}
    rows = _rows_by_id(client, state)

    assert set(rows) == {"project:p1", "task:t1", "task:s1"}
    assert all(row["_deleted"] is True for row in rows.values())
    assert state["known_ids"] == []


def test_missing_project_on_first_sync_fails_visibly():
    client = FakeAsanaClient()
    client.projects = {}
    with pytest.raises(AsanaNotFoundError, match="None of the selected"):
        _rows_by_id(client, {})


def test_workspace_selection_resolves_projects():
    client = FakeAsanaClient()
    rows = list(sync_asana(client, {}, workspace_id="w1"))
    assert {row["id"] for row in rows} == {"project:p1", "task:t1", "task:s1"}


def test_selection_is_required_and_mutually_exclusive():
    client = FakeAsanaClient()
    with pytest.raises(ValueError, match="Select what to ingest"):
        list(sync_asana(client, {}))
    with pytest.raises(ValueError, match="not both"):
        list(sync_asana(client, {}, project_ids=["p1"], workspace_id="w1"))
    with pytest.raises(TypeError, match="not a string"):
        list(sync_asana(client, {}, project_ids="p1"))


def test_renderers_produce_document_rows():
    project_row = _project_to_row(_project())
    task_row = _task_to_row(_task("t1", "Prepare launch"), [_comment("c1", "Approved")])

    assert project_row["id"] == "project:p1"
    assert "Ship the new product safely" in project_row["content"]
    assert task_row["id"] == "task:t1"
    assert "## Comments" in task_row["content"]
    assert "Asha" in task_row["content"]
    assert task_row["_deleted"] is False


def test_task_row_maps_to_a_searchable_cognee_document():
    from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

    row = _task_to_row(_task("t1", "Prepare launch"), [_comment("c1", "Approved")])
    data_id = uuid5(NAMESPACE_OID, row["id"])
    item = _build_document_data_item(
        SimpleNamespace(
            row_data=row,
            content_hash="content-hash",
            table_name=ASANA_TABLE_NAME,
        ),
        data_id,
        ASANA_SOURCE_NAME,
    )

    assert item.data_id == data_id
    assert item.system_metadata["source"] == ASANA_SOURCE_NAME
    assert item.system_metadata["external_id"] == "task:t1"
    assert item.system_metadata["url"].endswith("/t1")
    assert "Prepare launch" in item.data
    assert "Approved" in item.data


def test_story_fingerprint_is_stable_and_content_sensitive():
    first = [_comment("c1", "Alpha")]
    assert _story_fingerprint(first) == _story_fingerprint(deepcopy(first))
    assert _story_fingerprint(first) != _story_fingerprint([_comment("c1", "Beta")])


class ScriptedClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.params = []

    def get(self, url, params=None, timeout=None):
        self.params.append(dict(params or {}))
        return self.responses.pop(0)


def test_paginate_follows_opaque_offsets():
    client = ScriptedClient(
        [
            FakeResponse({"data": [{"gid": "a"}], "next_page": {"offset": "next-token"}}),
            FakeResponse({"data": [{"gid": "b"}], "next_page": None}),
        ]
    )

    assert [item["gid"] for item in _paginate(client, "tasks")] == ["a", "b"]
    assert client.params[1]["offset"] == "next-token"


def test_paginate_rejects_missing_or_repeated_offsets():
    missing = ScriptedClient([FakeResponse({"data": [], "next_page": {"uri": "x"}})])
    with pytest.raises(AsanaAPIError, match="omitted"):
        list(_paginate(missing, "tasks"))

    repeated = ScriptedClient(
        [
            FakeResponse({"data": [], "next_page": {"offset": "same"}}),
            FakeResponse({"data": [], "next_page": {"offset": "same"}}),
        ]
    )
    with pytest.raises(AsanaAPIError, match="repeated"):
        list(_paginate(repeated, "tasks"))


def test_api_get_retries_rate_limit(monkeypatch):
    client = ScriptedClient(
        [
            FakeResponse({"errors": []}, status_code=429, headers={"Retry-After": "0"}),
            FakeResponse({"data": {"gid": "p1"}}),
        ]
    )
    monkeypatch.setattr("cognee_community_connector_asana.asana.time.sleep", lambda _: None)

    assert _api_get(client, "projects/p1")["data"]["gid"] == "p1"
    assert len(client.params) == 2


def test_api_get_rejects_malformed_success():
    client = ScriptedClient([FakeResponse({"unexpected": True})])
    with pytest.raises(AsanaAPIError, match="invalid response"):
        _api_get(client, "projects/p1")


def test_make_session_uses_personal_access_token_as_bearer_auth():
    session = _make_session("test-personal-access-token")

    assert session.headers["Authorization"] == "Bearer test-personal-access-token"
    assert session.headers["Accept"] == "application/json"


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def test_source_declares_document_marker_and_merge(dlt_mod):
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    source = asana_source(project_ids=["p1"], client=FakeAsanaClient())
    resource = next(iter(source.resources.values()))
    disposition = resource.write_disposition
    if isinstance(disposition, dict):
        disposition = disposition.get("disposition")

    assert source.name == ASANA_SOURCE_NAME
    assert resource.name == ASANA_TABLE_NAME
    assert disposition == "merge"
    assert document_source_tag(source) == ASANA_SOURCE_NAME

    columns = resource.compute_table_schema()["columns"]
    assert columns["id"].get("primary_key") is True
    assert columns["_deleted"].get("hard_delete") is True


def _run_pipeline(dlt_mod, tmp_path, client):
    pipeline = dlt_mod.pipeline(
        pipeline_name="asana_test",
        destination=dlt_mod.destinations.sqlalchemy(
            f"sqlite:///{(tmp_path / 'asana.db').as_posix()}"
        ),
        dataset_name="asana_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(asana_source(project_ids=["p1"], client=client))
    return pipeline


def _read_ids(pipeline):
    with (
        pipeline.sql_client() as sql_client,
        sql_client.execute_query(f"SELECT id FROM {ASANA_TABLE_NAME}") as cursor,
    ):
        return {row[0] for row in cursor.fetchall()}


def test_dlt_merge_physically_removes_deleted_subtask(dlt_mod, tmp_path):
    client = FakeAsanaClient()
    _run_pipeline(dlt_mod, tmp_path, client)

    client.subtasks["t1"] = []
    pipeline = _run_pipeline(dlt_mod, tmp_path, client)

    assert _read_ids(pipeline) == {"project:p1", "task:t1"}
