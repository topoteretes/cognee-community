"""Offline tests for frame rendering and full-snapshot Miro sync."""

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
    MiroBoardNotFoundError,
    MiroSnapshotChangedError,
    _board_documents,
    _MiroRESTSource,
    _plain_text,
    _sync_rows,
    miro_source,
)


def _board(
    modified_at: str = "2026-10-01T10:00:00Z",
    *,
    board_id: str = "board-1",
    name: str = "Product workshop",
) -> dict:
    return {
        "id": board_id,
        "name": name,
        "modifiedAt": modified_at,
        "viewLink": f"https://miro.com/app/board/{board_id}/",
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
        *,
        item_error: Exception | None = None,
        board_errors: dict[str, Exception] | None = None,
        board_responses: dict[str, list[dict | Exception]] | None = None,
    ):
        self.boards = boards
        self.items = items
        self.item_error = item_error
        self.board_errors = board_errors or {}
        self.board_responses = board_responses or {}
        self.item_calls: list[str] = []
        self.get_board_calls: list[str] = []
        self.list_board_calls: list[dict] = []

    def list_boards(self, **kwargs):
        self.list_board_calls.append(kwargs)
        return deepcopy(self.boards)

    def get_board(self, board_id: str):
        self.get_board_calls.append(board_id)
        queued = self.board_responses.get(board_id)
        if queued:
            response = queued.pop(0)
            if isinstance(response, Exception):
                raise response
            return deepcopy(response)
        if error := self.board_errors.get(board_id):
            raise error
        board = next((board for board in self.boards if str(board.get("id")) == board_id), None)
        if board is None:
            raise MiroBoardNotFoundError(board_id)
        return deepcopy(board)

    def list_items(self, board_id: str):
        self.item_calls.append(board_id)
        if self.item_error:
            raise self.item_error
        return deepcopy(self.items[board_id])


class StubMiroAdapter(BaseAdapter):
    """Small requests adapter that exercises dlt REST extraction offline."""

    def __init__(self):
        self.requests: list[requests.PreparedRequest] = []

    def send(self, request, **kwargs):
        self.requests.append(request)
        response = Response()
        response.request = request
        response.url = request.url
        response.headers["Content-Type"] = "application/json"
        path = urlsplit(request.url).path

        if path == "/v2/boards/missing":
            response.status_code = 404
            payload = {"message": "not found"}
        elif path == "/v2/boards/forbidden":
            response.status_code = 403
            payload = {"message": "forbidden"}
        elif path == "/v2/boards/board-1":
            response.status_code = 200
            payload = _board()
        elif path == "/v2/boards":
            response.status_code = 200
            payload = {"data": [_board()], "total": 1}
        elif "cursor=next-page" in request.url:
            response.status_code = 200
            payload = {"data": [_item("second", "Second")]}
        else:
            response.status_code = 200
            payload = {"data": [_frame("f1", "Plan")], "cursor": "next-page"}

        response._content = json.dumps(payload).encode()
        return response

    def close(self):
        return None


def _session_with_stub() -> tuple[requests.Session, StubMiroAdapter]:
    session = requests.Session()
    adapter = StubMiroAdapter()
    session.mount("https://api.miro.com/", adapter)
    return session, adapter


def test_plain_text_strips_miro_html() -> None:
    assert _plain_text("<p>Hello&nbsp;<strong>world</strong></p><ul><li>Next</li></ul>") == (
        "Hello world\nNext"
    )


def test_declarative_rest_source_handles_auth_filters_direct_lookup_and_pagination() -> None:
    session, adapter = _session_with_stub()
    client = _MiroRESTSource("oauth-token", session=session)

    boards = client.list_boards(team_id="team-1", project_id="project-1")
    board = client.get_board("board-1")
    items = client.list_items("uXjVExampleBoardId=")

    assert [item["id"] for item in boards] == ["board-1"]
    assert board["id"] == "board-1"
    assert [item["id"] for item in items] == ["f1", "second"]
    assert len(adapter.requests) == 4
    assert all(
        request.headers["Authorization"] == "Bearer oauth-token" for request in adapter.requests
    )

    board_list_request = next(
        request for request in adapter.requests if urlsplit(request.url).path == "/v2/boards"
    )
    assert parse_qs(urlsplit(board_list_request.url).query) == {
        "limit": ["50"],
        "offset": ["0"],
        "project_id": ["project-1"],
        "sort": ["last_modified"],
        "team_id": ["team-1"],
    }

    direct_request = next(
        request
        for request in adapter.requests
        if urlsplit(request.url).path == "/v2/boards/board-1"
    )
    assert not urlsplit(direct_request.url).query

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


def test_direct_lookup_translates_only_404_to_board_not_found() -> None:
    session, _ = _session_with_stub()
    client = _MiroRESTSource("oauth-token", session=session)

    with pytest.raises(MiroBoardNotFoundError):
        client.get_board("missing")

    with pytest.raises(Exception) as forbidden:
        client.get_board("forbidden")
    assert not isinstance(forbidden.value, MiroBoardNotFoundError)


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


def test_unchanged_board_produces_a_complete_consistent_snapshot() -> None:
    client = FakeMiroClient(
        [_board()],
        {"board-1": [_frame("f1", "Plan"), _item("a", "A", parent_id="f1")]},
    )

    rows = list(_sync_rows(client))

    assert [row["id"] for row in rows] == ["board-1:frame:f1"]
    assert client.item_calls == ["board-1"]
    assert client.get_board_calls == ["board-1"]
    assert not any("_deleted" in row for row in rows)


def test_board_changing_once_is_retried_and_second_snapshot_is_used() -> None:
    first = _board("2026-10-01T10:00:00Z")
    second = _board("2026-10-02T10:00:00Z")
    client = FakeMiroClient(
        [first],
        {"board-1": [_frame("f1", "Plan"), _item("a", "A", parent_id="f1")]},
        board_responses={"board-1": [second, second]},
    )

    rows = list(_sync_rows(client))

    assert [row["id"] for row in rows] == ["board-1:frame:f1"]
    assert client.item_calls == ["board-1", "board-1"]
    assert client.get_board_calls == ["board-1", "board-1"]


def test_board_changing_twice_aborts_without_yielding_a_partial_snapshot() -> None:
    client = FakeMiroClient(
        [
            _board("2026-10-01T10:00:00Z"),
            _board("2026-10-01T10:00:00Z", board_id="board-2", name="Second board"),
        ],
        {
            "board-1": [_frame("f1", "One"), _item("a", "A", parent_id="f1")],
            "board-2": [_frame("f2", "Two"), _item("b", "B", parent_id="f2")],
        },
        board_responses={
            "board-1": [_board("2026-10-01T10:00:00Z")],
            "board-2": [
                _board("2026-10-02T10:00:00Z", board_id="board-2"),
                _board("2026-10-03T10:00:00Z", board_id="board-2"),
            ],
        },
    )
    rows = _sync_rows(client)

    with pytest.raises(MiroSnapshotChangedError, match="changed during both snapshot attempts"):
        next(rows)


def test_failed_item_walk_aborts_before_any_snapshot_rows_are_yielded() -> None:
    client = FakeMiroClient(
        [_board()],
        {"board-1": []},
        item_error=RuntimeError("pagination failed"),
    )

    with pytest.raises(RuntimeError, match="pagination failed"):
        next(_sync_rows(client))


def test_explicit_board_ids_use_direct_lookup_deduplicate_and_skip_confirmed_404() -> None:
    board_2 = _board(board_id="board-2", name="Second board")
    client = FakeMiroClient(
        [_board(), board_2],
        {
            "board-1": [_frame("f1", "One"), _item("a", "A", parent_id="f1")],
            "board-2": [_frame("f2", "Two"), _item("b", "B", parent_id="f2")],
        },
        board_errors={"missing": MiroBoardNotFoundError("missing")},
    )

    rows = list(_sync_rows(client, selected_ids={"board-2", "board-1", "missing"}))

    assert {row["id"] for row in rows} == {"board-1:frame:f1", "board-2:frame:f2"}
    assert client.list_board_calls == []
    assert client.get_board_calls == ["board-1", "board-2", "missing", "board-1", "board-2"]

    duplicate_client = FakeMiroClient(
        [_board()],
        {"board-1": [_frame("f1", "One"), _item("a", "A", parent_id="f1")]},
    )
    duplicate_resource = miro_source(
        board_ids=["board-1", "board-1"],
        client=duplicate_client,
    )
    assert [row["id"] for row in duplicate_resource] == ["board-1:frame:f1"]
    assert duplicate_client.get_board_calls == ["board-1", "board-1"]


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_non_404_direct_lookup_errors_abort_snapshot(status: int) -> None:
    response = Response()
    response.status_code = status
    error = requests.HTTPError(f"HTTP {status}", response=response)
    client = FakeMiroClient([], {}, board_errors={"board-1": error})

    with pytest.raises(requests.HTTPError, match=str(status)):
        next(_sync_rows(client, selected_ids={"board-1"}))


def test_team_and_project_filters_are_used_only_for_discovery() -> None:
    client = FakeMiroClient(
        [_board()],
        {"board-1": [_frame("f1", "Plan"), _item("a", "A", parent_id="f1")]},
    )

    list(_sync_rows(client, team_id="team-1", project_id="project-1"))

    assert client.list_board_calls == [{"team_id": "team-1", "project_id": "project-1"}]


def test_source_declares_document_mode_and_replace_schema() -> None:
    resource = miro_source(client=FakeMiroClient([], {}))

    assert resource.name == "miro_documents"
    assert resource.cognee_document_source == MIRO_SOURCE_NAME

    schema = resource.compute_table_schema()
    write_disposition = schema.get("write_disposition")
    if isinstance(write_disposition, dict):
        write_disposition = write_disposition.get("disposition")
    assert write_disposition == "replace"
    assert schema["columns"]["id"].get("primary_key") is True
    assert "_deleted" not in schema["columns"]


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


def test_dlt_replace_removes_deleted_frame_and_failed_snapshot_preserves_table(tmp_path) -> None:
    import dlt

    pipelines_dir = str(tmp_path / "pipelines")
    database_path = tmp_path / "miro.db"

    def sync(client: FakeMiroClient, board_ids=None) -> list[str]:
        pipeline = dlt.pipeline(
            pipeline_name="miro_e2e_test",
            destination=dlt.destinations.sqlalchemy(f"sqlite:///{database_path}"),
            dataset_name="miro_e2e",
            pipelines_dir=pipelines_dir,
        )
        pipeline.run(miro_source(client=client, board_ids=board_ids))
        with pipeline.sql_client() as sql_client:
            rows = sql_client.execute_sql("SELECT id FROM miro_documents ORDER BY id")
        return [row[0] for row in rows]

    initial = FakeMiroClient(
        [_board()],
        {
            "board-1": [
                _frame("f1", "Keep"),
                _frame("f2", "Delete"),
                _item("a", "A", parent_id="f1"),
                _item("b", "B", parent_id="f2"),
            ]
        },
    )
    assert sync(initial) == ["board-1:frame:f1", "board-1:frame:f2"]

    deleted = FakeMiroClient(
        [_board("2026-10-02T10:00:00Z")],
        {"board-1": [_frame("f1", "Keep"), _item("a", "A", parent_id="f1")]},
    )
    assert sync(deleted) == ["board-1:frame:f1"]

    failed = FakeMiroClient(
        [_board("2026-10-03T10:00:00Z")],
        {"board-1": []},
        item_error=RuntimeError("transient failure"),
    )
    with pytest.raises(Exception, match="transient failure"):
        sync(failed)

    pipeline = dlt.pipeline(
        pipeline_name="miro_e2e_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{database_path}"),
        dataset_name="miro_e2e",
        pipelines_dir=pipelines_dir,
    )
    with pipeline.sql_client() as sql_client:
        rows = sql_client.execute_sql("SELECT id FROM miro_documents ORDER BY id")
    assert [row[0] for row in rows] == ["board-1:frame:f1"]

    removed = FakeMiroClient(
        [],
        {},
        board_errors={"board-1": MiroBoardNotFoundError("board-1")},
    )
    assert sync(removed, board_ids=["board-1"]) == []


def test_cognee_add_routes_miro_documents_and_forgets_one_deleted_frame(
    tmp_path, monkeypatch
) -> None:
    """Exercise full-snapshot document routing and orphan cleanup through cognee.add()."""
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
            if isinstance(item.system_metadata, dict)
            and item.system_metadata.get("source") == "miro"
        ]

    async def add(client):
        await cognee.add(
            miro_source(client=client),
            dataset_name=dataset_name,
            primary_key="id",
            write_disposition="replace",
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
                item.system_metadata["external_id"]: item for item in initial
            }
            assert set(initial_by_external_id) == {
                "board-1:frame:f1",
                "board-1:frame:f2",
            }
            assert all(not is_dlt_sourced(item) for item in initial)

            client.boards = [_board("2026-10-02T10:00:00Z")]
            client.items["board-1"] = [
                _frame("f1", "Keep"),
                _item("a", "Alpha", parent_id="f1"),
            ]
            await add(client)

            final = await miro_data()
            assert len(final) == 1
            assert final[0].system_metadata["external_id"] == "board-1:frame:f1"
            assert final[0].id == initial_by_external_id["board-1:frame:f1"].id

            client.boards = []
            client.items = {}
            await add(client)

            assert await miro_data() == []
        finally:
            await cognee.prune.prune_data()
            await cognee.prune.prune_system(metadata=True)

    asyncio.run(scenario())
