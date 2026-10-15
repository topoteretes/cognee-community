"""Scope, rendering and the dlt resource, driven by a fake Wrike client (no network)."""

import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

from cognee_community_connector_wrike.wrike import (
    _iter_rows,
    html_to_text,
    wrike_source,
)

ROOT, RB, SPACE, PROJECT, BACKEND, DELETED = "ROOT", "RB", "SPACE", "PROJ", "BACK", "GONE"

FOLDERS = [
    {"id": ROOT, "title": "Root", "scope": "WsRoot", "childIds": [SPACE]},
    {"id": RB, "title": "Recycle Bin", "scope": "RbRoot", "childIds": [DELETED]},
    {"id": SPACE, "title": "Space", "scope": "WsFolder", "childIds": [PROJECT]},
    {"id": PROJECT, "title": "Project", "scope": "WsFolder", "childIds": [BACKEND]},
    {"id": BACKEND, "title": "Backend", "scope": "WsFolder", "childIds": []},
    {"id": DELETED, "title": "Old", "scope": "RbFolder", "childIds": []},
]
FOLDER_DETAILS = {
    SPACE: {"id": SPACE, "title": "Space", "permalink": "https://w/s"},
    PROJECT: {
        "id": PROJECT,
        "title": "Project",
        "description": "<p>Launch the <b>shop</b></p>",
        "project": {"customStatusId": "S_SPACE", "ownerIds": ["U1"]},
        "customFields": [{"id": "CF_BUDGET", "value": "8000"}],
    },
    BACKEND: {"id": BACKEND, "title": "Backend"},
}
DB_TASK = {
    "id": "T_DB",
    "title": "Set up database",
    "parentIds": [BACKEND],
    "superTaskIds": [],
    "customStatusId": "S_SPACE",
    "status": "Active",
    "responsibleIds": ["U2", "U1"],
    "importance": "High",
    "dates": {"type": "Planned", "start": "2026-10-12T09:00:00", "due": "2026-10-15T17:00:00"},
    "customFields": [
        {"id": "CF_PRIORITY", "value": "High"},
        {"id": "CF_BUDGET", "value": "2000"},
        {"id": "CF_DELETED", "value": "x"},
        {"id": "CF_PRIORITY_EMPTY", "value": ""},
    ],
    "description": 'Schema for <a href="/u">@Ana Lee</a>:<br />users<ul><li>orders</li></ul>',
    "permalink": "https://w/t_db",
}
SUBTASK = {
    "id": "T_SUB",
    "title": "Index orders",
    "parentIds": [ROOT],
    "superTaskIds": ["T_DB"],
    "status": "Active",
    "dates": {"type": "Backlog"},
}
COMMENTS = {
    "T_DB": [
        {"id": "C2", "authorId": "BOT", "createdDate": "2026-10-13T08:00:00Z", "text": "Due soon"},
        {
            "id": "C1",
            "authorId": "U1",
            "createdDate": "2026-10-12T10:00:00Z",
            "text": "<p>UUIDs</p>",
        },
        {"id": "C3", "authorId": "U1", "createdDate": "2026-10-12T11:00:00Z", "text": "<br />"},
    ]
}


class FakeWrike:
    """Answers the paths the connector calls, and records them."""

    def __init__(self, tasks_by_folder=None):
        self.calls = []
        self.tasks_by_folder = tasks_by_folder or {
            ROOT: [SUBTASK, DB_TASK],
            SPACE: [SUBTASK, DB_TASK],
            PROJECT: [SUBTASK, DB_TASK],
            BACKEND: [DB_TASK],
        }

    def get(self, path, **params):
        self.calls.append((path, params))
        if path == "/folders":
            return FOLDERS
        if path == "/workflows":
            return [{"customStatuses": [{"id": "S_ACCOUNT", "name": "New"}]}]
        if path == "/spaces":
            return [{"id": SPACE}]
        if path == f"/spaces/{SPACE}/workflows":
            return [{"customStatuses": [{"id": "S_SPACE", "name": "In review"}]}]
        if path == "/contacts":
            return [
                {"id": "U1", "firstName": "Ana", "lastName": "Lee", "type": "Person"},
                {"id": "U2", "firstName": "Bo", "type": "Person"},
                {"id": "BOT", "firstName": "Wrike", "lastName": "Bot", "type": "Robot"},
            ]
        if path == "/customfields":
            return [
                {"id": "CF_PRIORITY", "title": "Priority", "type": "DropDown"},
                {"id": "CF_PRIORITY_EMPTY", "title": "Impact", "type": "DropDown"},
                {
                    "id": "CF_BUDGET",
                    "title": "Budget",
                    "type": "Currency",
                    "settings": {"currency": "USD"},
                },
            ]
        if path.startswith("/folders/") and path.endswith("/tasks"):
            return self.tasks_by_folder[path.split("/")[2]]
        if path.startswith("/folders/"):
            return [FOLDER_DETAILS[fid] for fid in path.split("/")[2].split(",")]
        if path.startswith("/tasks/") and path.endswith("/comments"):
            return COMMENTS.get(path.split("/")[2], [])
        raise AssertionError(f"unexpected call {path}")


def rows_by_id(folder_ids=None, client=None):
    return {row["id"]: row for row in _iter_rows(client or FakeWrike(), folder_ids)}


def test_task_document_uses_names_not_ids():
    content = rows_by_id()["task:T_DB"]["content"]
    assert content == (
        "Folder: Space / Project / Backend\n"
        "Status: In review\n"
        "Assignees: Ana Lee, Bo\n"
        "Importance: High\n"
        "Start: 2026-10-12\n"
        "Due: 2026-10-15\n"
        "Budget: 2000 USD\n"
        "Priority: High\n"
        "\n"
        "Schema for @Ana Lee:\n"
        "users\n"
        "- orders\n"
        "\n"
        "Comments:\n"
        "- Ana Lee (2026-10-12): UUIDs\n"
        "- Wrike Bot (2026-10-13): Due soon"
    )


def test_row_shape():
    row = rows_by_id()["task:T_DB"]
    assert row["title"] == "Set up database"
    assert row["url"] == "https://w/t_db"
    assert row["_deleted"] is False


def test_rendering_is_deterministic():
    assert list(_iter_rows(FakeWrike(), None)) == list(_iter_rows(FakeWrike(), None))


def test_subtask_names_its_parent_task():
    content = rows_by_id()["task:T_SUB"]["content"]
    assert content.splitlines()[0] == "Parent task: Set up database"
    assert "Folder:" not in content  # its parentIds is the account root


def test_folder_rows_exclude_root_and_recycle_bin():
    rows = rows_by_id()
    assert {r for r in rows if r.startswith("folder:")} == {
        "folder:SPACE",
        "folder:PROJ",
        "folder:BACK",
    }
    assert rows["folder:PROJ"]["content"] == (
        "Path: Space / Project\nProject status: In review\nOwners: Ana Lee\n"
        "Budget: 8000 USD\n\nLaunch the shop"
    )


def test_folder_ids_limit_the_scope():
    client = FakeWrike()
    rows = rows_by_id([BACKEND], client)
    assert set(rows) == {"folder:BACK", "task:T_DB"}
    task_calls = [(p, q) for p, q in client.calls if p.endswith("/tasks")]
    assert task_calls == [
        (
            f"/folders/{BACKEND}/tasks",
            {
                "descendants": True,
                "subTasks": True,
                "fields": [
                    "description",
                    "customFields",
                    "parentIds",
                    "responsibleIds",
                    "superTaskIds",
                ],
                "pageSize": 1000,
            },
        )
    ]


def test_overlapping_folders_yield_each_task_once():
    ids = [row["id"] for row in _iter_rows(FakeWrike(), [PROJECT, BACKEND])]
    assert ids.count("task:T_DB") == 1
    assert ids.count("folder:BACK") == 1


@pytest.mark.parametrize("folder_id", ["NOPE", DELETED])
def test_unknown_or_deleted_folder_is_a_config_error(folder_id):
    with pytest.raises(ValueError, match=folder_id):
        list(_iter_rows(FakeWrike(), [folder_id]))


def test_html_to_text():
    assert html_to_text(None) == ""
    assert html_to_text("a<br />b<p>c</p><p></p><p>d &amp; e</p>") == "a\nb\nc\n\nd & e"


def test_source_is_a_merge_document_resource():
    resource = wrike_source(client=FakeWrike())
    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == "wrike"
    assert resource.write_disposition == "merge"
    assert resource.columns["_deleted"]["hard_delete"] is True
    assert {row["id"] for row in resource} == set(rows_by_id())


def test_source_needs_credentials(monkeypatch):
    for name in (
        "WRIKE_ACCESS_TOKEN",
        "WRIKE_CLIENT_ID",
        "WRIKE_CLIENT_SECRET",
        "WRIKE_TOKEN_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="credentials required"):
        wrike_source()
    with pytest.raises(ValueError, match="OAuth needs all"):
        wrike_source(client_id="id")


def test_building_an_oauth_source_does_not_spend_the_refresh_token(tmp_path):
    token_file = tmp_path / "t.json"
    token_file.write_text('{"refresh_token": "r0"}')
    wrike_source(client_id="id", client_secret="s", token_path=token_file)
    assert token_file.read_text() == '{"refresh_token": "r0"}'


def test_folder_ids_from_env(monkeypatch):
    monkeypatch.setenv("WRIKE_FOLDER_IDS", f" {BACKEND} , ")
    assert {row["id"] for row in wrike_source(client=FakeWrike())} == {"folder:BACK", "task:T_DB"}
