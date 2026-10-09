"""Offline tests for frame rendering and incremental Miro sync."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from requests.adapters import BaseAdapter
from requests.models import Response

from cognee_community_connector_miro.miro import (
    MIRO_SOURCE_NAME,
    _board_documents,
    _MiroRESTSource,
    _plain_text,
    _sync_rows,
    miro_source,
)


def _board(modified_at: str = "2026-10-01T10:00:00Z") -> dict:
    return {
        "id": "board-1",
        "name": "Product workshop",
        "modifiedAt": modified_at,
        "viewLink": "https://miro.com/app/board/board-1/",
    }


def _frame(frame_id: str, title: str, *, x: int = 0, y: int = 0) -> dict:
    return {
        "id": frame_id,
        "type": "frame",
        "data": {"title": title},
        "position": {"x": x, "y": y},
    }


def _item(
    item_id: str,
    text: str,
    *,
    item_type: str = "sticky_note",
    parent_id: str | None = None,
    x: int = 0,
    y: int = 0,
) -> dict:
    return {
        "id": item_id,
        "type": item_type,
        "data": {"content": text},
        "parent": {"id": parent_id} if parent_id else None,
        "position": {"x": x, "y": y},
    }


class FakeMiroClient:
    def __init__(
        self,
        boards: list[dict],
        items: dict[str, list[dict]],
        error: Exception | None = None,
    ):
        self.boards = boards
        self.items = items
        self.error = error
        self.item_calls: list[str] = []

    def list_boards(self, **kwargs):
        return deepcopy(self.boards)

    def list_items(self, board_id: str):
        self.item_calls.append(board_id)
        if self.error:
            raise self.error
        return deepcopy(self.items[board_id])


class StubMiroAdapter(BaseAdapter):
    """Small requests adapter that exercises dlt REST pagination offline."""

    def __init__(self):
        self.requests: list[requests.PreparedRequest] = []

    def send(self, request, **kwargs):
        self.requests.append(request)
        response = Response()
        response.status_code = 200
        response.request = request
        response.url = request.url
        response.headers["Content-Type"] = "application/json"

        if "/boards?" in request.url:
            payload = {"data": [_board()], "total": 1}
        elif "cursor=next-page" in request.url:
            payload = {"data": [_item("second", "Second")]}
        else:
            payload = {"data": [_frame("f1", "Plan")], "cursor": "next-page"}
        response._content = json.dumps(payload).encode()
        return response

    def close(self):
        return None


def test_plain_text_strips_miro_html() -> None:
    assert _plain_text("<p>Hello&nbsp;<strong>world</strong></p><ul><li>Next</li></ul>") == (
        "Hello world\nNext"
    )


def test_declarative_rest_source_handles_auth_and_cursor_pagination() -> None:
    session = requests.Session()
    adapter = StubMiroAdapter()
    session.mount("https://api.miro.com/", adapter)
    client = _MiroRESTSource("oauth-token", session=session)

    boards = client.list_boards(team_id="team-1")
    items = client.list_items("uXjVExampleBoardId=")

    assert [board["id"] for board in boards] == ["board-1"]
    assert [item["id"] for item in items] == ["f1", "second"]
    assert len(adapter.requests) == 3
    assert all(
        request.headers["Authorization"] == "Bearer oauth-token" for request in adapter.requests
    )

    item_requests = [request for request in adapter.requests if "/items?" in request.url]
    assert len(item_requests) == 2
    assert all(
        urlsplit(request.url).path == "/v2/boards/uXjVExampleBoardId=/items"
        for request in item_requests
    )
    assert all(
        "parent_item_id" not in parse_qs(urlsplit(request.url).query) for request in item_requests
    )
    assert parse_qs(urlsplit(item_requests[0].url).query) == {"limit": ["50"]}
    assert parse_qs(urlsplit(item_requests[1].url).query) == {
        "cursor": ["next-page"],
        "limit": ["50"],
    }


def test_board_documents_group_by_frame_and_keep_unframed_items() -> None:
    items = [
        _frame("f1", "Discovery"),
        _item("later", "Second", parent_id="f1", x=10, y=20),
        _item("first", "<b>First</b>", item_type="text", parent_id="f1", x=20, y=10),
        _item("canvas", "Outside", item_type="shape", x=5, y=5),
        {"id": "image", "type": "image", "data": {"title": "ignored"}},
    ]

    documents = _board_documents(_board(), items)

    assert [row["id"] for row in documents] == [
        "board-1:frame:f1",
        "board-1:unframed",
    ]
    assert documents[0]["title"] == "Product workshop — Discovery"
    assert documents[0]["content"].index("First") < documents[0]["content"].index("Second")
    assert "Outside" in documents[1]["content"]
    assert "ignored" not in "".join(row["content"] for row in documents)


def test_initial_sync_then_unchanged_board_skips_item_walk() -> None:
    client = FakeMiroClient(
        [_board()],
        {"board-1": [_frame("f1", "Plan"), _item("a", "A", parent_id="f1")]},
    )
    state: dict = {}

    first_rows = list(_sync_rows(client, state))
    second_rows = list(_sync_rows(client, state))

    assert [row["id"] for row in first_rows] == ["board-1:frame:f1"]
    assert second_rows == []
    assert client.item_calls == ["board-1"]


def test_changed_board_emits_only_changed_frame() -> None:
    client = FakeMiroClient(
        [_board()],
        {
            "board-1": [
                _frame("f1", "One"),
                _frame("f2", "Two"),
                _item("a", "A", parent_id="f1"),
                _item("b", "B", parent_id="f2"),
            ]
        },
    )
    state: dict = {}
    list(_sync_rows(client, state))

    client.boards = [_board("2026-10-02T10:00:00Z")]
    client.items["board-1"][2]["data"]["content"] = "A changed"
    rows = list(_sync_rows(client, state))

    assert [row["id"] for row in rows] == ["board-1:frame:f1"]


def test_moving_item_rewrites_destination_and_deletes_empty_source_frame() -> None:
    client = FakeMiroClient(
        [_board()],
        {
            "board-1": [
                _frame("f1", "Old"),
                _frame("f2", "New"),
                _item("a", "Move me", parent_id="f1"),
                _item("b", "Keep me", parent_id="f2"),
            ]
        },
    )
    state: dict = {}
    list(_sync_rows(client, state))

    client.boards = [_board("2026-10-02T10:00:00Z")]
    client.items["board-1"][2]["parent"] = {"id": "f2"}
    rows = list(_sync_rows(client, state))

    assert {row["id"] for row in rows} == {"board-1:frame:f1", "board-1:frame:f2"}
    tombstone = next(row for row in rows if row["id"] == "board-1:frame:f1")
    assert tombstone == {"id": "board-1:frame:f1", "_deleted": True}


def test_removed_board_emits_document_tombstones() -> None:
    client = FakeMiroClient(
        [_board()],
        {"board-1": [_frame("f1", "Plan"), _item("a", "A", parent_id="f1")]},
    )
    state: dict = {}
    list(_sync_rows(client, state, selected_ids={"board-1"}))

    client.boards = []
    rows = list(_sync_rows(client, state, selected_ids={"board-1"}))

    assert rows == [{"id": "board-1:frame:f1", "_deleted": True}]
    assert state == {"boards": {}}


def test_failed_item_walk_does_not_advance_state() -> None:
    old_state = {
        "boards": {
            "board-1": {
                "modified_at": "2026-10-01T10:00:00Z",
                "documents": {"board-1:frame:f1": "old-hash"},
            }
        }
    }
    state = deepcopy(old_state)
    client = FakeMiroClient(
        [_board("2026-10-02T10:00:00Z")],
        {"board-1": []},
        error=RuntimeError("pagination failed"),
    )

    with pytest.raises(RuntimeError, match="pagination failed"):
        list(_sync_rows(client, state))

    assert state == old_state


def test_source_declares_document_mode_and_merge_delete_schema() -> None:
    resource = miro_source(client=FakeMiroClient([], {}))

    assert resource.name == "miro_documents"
    assert resource.cognee_document_source == MIRO_SOURCE_NAME

    schema = resource.compute_table_schema()
    write_disposition = schema.get("write_disposition")
    if isinstance(write_disposition, dict):
        write_disposition = write_disposition.get("disposition")
    assert write_disposition == "merge"
    assert schema["columns"]["id"].get("primary_key") is True
    assert schema["columns"]["_deleted"].get("hard_delete") is True


@pytest.mark.parametrize("board_ids", [[], (), [""], ["  "]])
def test_source_rejects_empty_explicit_board_selection(board_ids) -> None:
    with pytest.raises(ValueError, match="board_ids cannot be empty"):
        miro_source(board_ids=board_ids, client=FakeMiroClient([], {}))


def test_source_rejects_single_string_as_board_ids() -> None:
    with pytest.raises(TypeError, match="iterable of board IDs"):
        miro_source(board_ids="board-1", client=FakeMiroClient([], {}))


def test_source_requires_token_without_injected_client(monkeypatch) -> None:
    monkeypatch.delenv("SOURCES__MIRO__ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("MIRO_ACCESS_TOKEN", raising=False)
    with pytest.raises(ValueError, match="dlt secret provider"):
        miro_source()


def test_source_reads_access_token_from_dlt_secret_provider(monkeypatch) -> None:
    monkeypatch.delenv("MIRO_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("SOURCES__MIRO__ACCESS_TOKEN", "dlt-managed-token")

    resource = miro_source()

    assert resource.name == "miro_documents"


def test_dlt_merge_removes_deleted_frame(tmp_path) -> None:
    import dlt

    pipelines_dir = str(tmp_path / "pipelines")
    database_path = tmp_path / "miro.db"

    def sync(client: FakeMiroClient) -> list[str]:
        pipeline = dlt.pipeline(
            pipeline_name="miro_e2e_test",
            destination=dlt.destinations.sqlalchemy(f"sqlite:///{database_path}"),
            dataset_name="miro_e2e",
            pipelines_dir=pipelines_dir,
        )
        pipeline.run(miro_source(client=client))
        with pipeline.sql_client() as sql_client:
            rows = sql_client.execute_sql("SELECT id FROM miro_documents ORDER BY id")
        return [row[0] for row in rows]

    initial = FakeMiroClient(
        [_board()],
        {"board-1": [_frame("f1", "Plan"), _item("a", "A", parent_id="f1")]},
    )
    assert sync(initial) == ["board-1:frame:f1"]

    emptied = FakeMiroClient([_board("2026-10-02T10:00:00Z")], {"board-1": []})
    assert sync(emptied) == []


def test_cognee_add_routes_miro_documents_and_forgets_one_deleted_frame(
    tmp_path, monkeypatch
) -> None:
    """Exercise document routing and orphan cleanup through cognee.add()."""
    import cognee
    from cognee.modules.data.methods import get_authorized_existing_datasets
    from cognee.modules.data.methods.get_dataset_data import get_dataset_data
    from cognee.modules.users.methods import get_default_user
    from cognee.tasks.ingestion.dlt_utils import is_dlt_sourced

    dataset_name = "miro_connector_integration_test"
    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
    monkeypatch.chdir(tmp_path)
    cognee.config.data_root_directory(str(tmp_path / "data"))
    cognee.config.system_root_directory(str(tmp_path / "system"))
    cognee.config.set_relational_db_config({"db_provider": "sqlite"})

    async def miro_data():
        user = await get_default_user()
        datasets = await get_authorized_existing_datasets(
            user=user,
            permission_type="write",
            datasets=[dataset_name],
        )
        if not datasets:
            return []
        data = await get_dataset_data(datasets[0].id)
        return [
            item
            for item in data
            if isinstance(item.external_metadata, dict)
            and item.external_metadata.get("source") == "miro"
        ]

    async def add(client):
        await cognee.add(
            miro_source(client=client),
            dataset_name=dataset_name,
            primary_key="id",
            write_disposition="merge",
            max_rows_per_table=0,
        )

    async def scenario():
        await cognee.prune.prune_data()
        await cognee.prune.prune_system(metadata=True)
        try:
            client = FakeMiroClient(
                [_board()],
                {
                    "board-1": [
                        _frame("f1", "Keep"),
                        _frame("f2", "Delete"),
                        _item("a", "Alpha", parent_id="f1"),
                        _item("b", "Bravo", parent_id="f2"),
                    ]
                },
            )
            await add(client)

            initial = await miro_data()
            assert len(initial) == 2
            initial_by_external_id = {
                item.external_metadata["external_id"]: item for item in initial
            }
            assert set(initial_by_external_id) == {
                "board-1:frame:f1",
                "board-1:frame:f2",
            }
            assert all(not is_dlt_sourced(item.external_metadata) for item in initial)

            client.boards = [_board("2026-10-02T10:00:00Z")]
            client.items["board-1"] = [
                _frame("f1", "Keep"),
                _item("a", "Alpha", parent_id="f1"),
            ]
            await add(client)

            final = await miro_data()
            assert len(final) == 1
            assert final[0].external_metadata["external_id"] == "board-1:frame:f1"
            assert final[0].id == initial_by_external_id["board-1:frame:f1"].id
        finally:
            await cognee.prune.prune_data()
            await cognee.prune.prune_system(metadata=True)

    asyncio.run(scenario())
