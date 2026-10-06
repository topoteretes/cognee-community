"""Unit tests for the Trello dlt connector.

The Trello REST API is fully mocked via ``FakeTrello`` — no network, no
credentials, so these run in CI. Coverage:

  - cards are rendered to documents with description, list, comments, and
    checklists; boards to overview documents with lists and labels
  - backfill yields the board document and every open card, and records
    per-board/per-card state
  - the incremental cursor is the board actions feed: only cards touched by
    actions since the last action id are re-emitted
  - cards that appear in the sweep beyond the actions window are still caught
  - cards that vanish from the open-card sweep (deleted or archived) become
    hard-delete markers (forget-on-delete); removing a board from the config
    tombstones its documents
  - a board whose fetch fails is skipped without mass-deleting its documents,
    and a card whose fetch fails is retried (not deleted, not re-emitted)
  - the dlt resource is wired with merge + id PK + the hard_delete column and
    declares the document-source marker
  - a real dlt merge removes the marked row (end-to-end forget-on-delete) and
    persists the incremental state across pipeline runs

The end-to-end "deletion removes it from memory" guarantee is provided by the
existing ``orphan_cleanup`` path in cognee core; here we prove the connector
emits the markers that drive it, and that dlt acts on them.
"""

import pytest

from cognee_community_connector_trello.trello import (
    TRELLO_SOURCE_NAME,
    sync_boards,
    trello_source,
)

BOARD_ID = "b1"
OTHER_BOARD_ID = "b2"


def _board_config(**overrides):
    """A full fake board: overview, lists, labels, cards, comments, actions."""
    config = {
        "board": {
            "id": BOARD_ID,
            "name": "Launch",
            "desc": "Launch plan",
            "url": "https://trello.com/b/b1",
        },
        "lists": [{"id": "l1", "name": "Backlog"}, {"id": "l2", "name": "Doing"}],
        "labels": [{"name": "bug", "color": "red"}],
        "open_cards": ["c1", "c2"],
        "cards": {
            "c1": {
                "id": "c1",
                "name": "Write spec",
                "desc": "The spec body",
                "shortUrl": "https://trello.com/c/c1",
                "idList": "l2",
            },
            "c2": {
                "id": "c2",
                "name": "Ship it",
                "desc": "",
                "shortUrl": "https://trello.com/c/c2",
                "idList": "l1",
            },
        },
        "comments": {
            "c1": [{"data": {"text": "First comment"}, "memberCreator": {"username": "alice"}}]
        },
        "checklists": {
            "c1": {
                "name": "Steps",
                "checkItems": [
                    {"name": "draft", "state": "complete"},
                    {"name": "review", "state": "incomplete"},
                ],
            }
        },
        "actions_initial": [
            # Newest first, as the Trello actions feed returns them.
            {
                "id": "act2",
                "date": "2026-10-01T11:00:00.000Z",
                "type": "createCard",
                "data": {"card": {"id": "c2"}},
            },
            {
                "id": "act1",
                "date": "2026-10-01T10:00:00.000Z",
                "type": "createCard",
                "data": {"card": {"id": "c1"}},
            },
        ],
        "actions_after": [],
    }
    config.update(overrides)
    return config


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        if isinstance(self._payload, Exception):
            raise self._payload

    def json(self):
        return self._payload


class FakeTrello:
    """Minimal stand-in for a ``requests`` session hitting the Trello REST API."""

    def __init__(self, boards):
        # boards: {board_id: board_config}
        self.boards = boards

    def get(self, url, params=None):
        params = params or {}
        path = url.split("/1/", 1)[1]

        if path.startswith("boards/"):
            board_id = path.split("/")[1]
            board = self.boards.get(board_id)
            if board is None:
                return _Resp(ValueError(f"unknown board {board_id}"))
            if path.endswith("/lists"):
                return _Resp(board["lists"])
            if path.endswith("/labels"):
                return _Resp(board["labels"])
            if path.endswith("/cards"):
                return _Resp([{"id": cid} for cid in board["open_cards"]])
            if path.endswith("/actions"):
                if params.get("since"):
                    return _Resp(board["actions_after"])
                return _Resp(board["actions_initial"])
            if path == f"boards/{board_id}":  # overview fetch (no sub-resource)
                return _Resp(board["board"])

        if path.startswith("cards/"):
            card_id = path.split("/")[1]
            for board in self.boards.values():
                if card_id in board["cards"]:
                    if path.endswith("/actions"):
                        return _Resp(board["comments"].get(card_id, []))
                    if path.endswith("/checklists"):
                        # The real API returns an array of checklists.
                        return _Resp(
                            [board["checklists"][card_id]] if card_id in board["checklists"] else []
                        )
                    payload = board["cards"][card_id]
                    return _Resp(payload) if not isinstance(payload, Exception) else _Resp(payload)
            return _Resp(ValueError(f"unknown card {card_id}"))

        return _Resp(ValueError(f"unexpected URL: {url}"))


def _ids(rows):
    return [row["id"] for row in rows]


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------
def test_backfill_emits_board_document_and_all_cards():
    state = {}
    rows = list(sync_boards(FakeTrello({BOARD_ID: _board_config()}), state, [BOARD_ID]))

    assert _ids(rows) == [BOARD_ID, "c1", "c2"]
    assert all(row["_deleted"] is False for row in rows)

    board_row = rows[0]
    assert board_row["title"] == "Launch"
    assert board_row["url"] == "https://trello.com/b/b1"
    assert "Launch plan" in board_row["content"]
    assert "- Doing" in board_row["content"]  # lists rendered
    assert "- bug (red)" in board_row["content"]  # labels rendered

    card_row = rows[1]
    assert card_row["title"] == "Write spec"
    assert card_row["url"] == "https://trello.com/c/c1"
    assert "The spec body" in card_row["content"]
    assert "In list: Doing" in card_row["content"]  # idList resolved to a name
    assert "- **alice**: First comment" in card_row["content"]
    assert "- [x] draft" in card_row["content"]
    assert "- [ ] review" in card_row["content"]

    # State recorded: per-board cursor + board hash, per-card content hashes.
    assert state["boards"][BOARD_ID]["last_action_id"] == "act2"
    assert set(state["cards"]) == {"c1", "c2"}
    assert state["cards"]["c1"]["board"] == BOARD_ID


def test_backfill_card_without_comments_or_checklists_renders_minimal_content():
    state = {}
    rows = list(sync_boards(FakeTrello({BOARD_ID: _board_config()}), state, [BOARD_ID]))

    assert rows[2]["content"] == "Ship it\n\nIn list: Backlog"


# ---------------------------------------------------------------------------
# Incremental (actions-feed cursor)
# ---------------------------------------------------------------------------
def test_incremental_emits_only_cards_touched_by_actions():
    state = {}
    list(sync_boards(FakeTrello({BOARD_ID: _board_config()}), state, [BOARD_ID]))

    board = _board_config(
        actions_after=[
            {
                "id": "act3",
                "date": "2026-10-02T09:00:00.000Z",
                "type": "commentCard",
                "data": {"card": {"id": "c1"}},
            }
        ],
        comments={
            "c1": [
                {"data": {"text": "First comment"}, "memberCreator": {"username": "alice"}},
                {"data": {"text": "Second comment"}, "memberCreator": {"username": "bob"}},
            ]
        },
    )
    rows = list(sync_boards(FakeTrello({BOARD_ID: board}), state, [BOARD_ID]))

    assert _ids(rows) == ["c1"]
    assert "Second comment" in rows[0]["content"]
    assert "First comment" in rows[0]["content"]  # full document re-rendered
    assert state["boards"][BOARD_ID]["last_action_id"] == "act3"


def test_incremental_no_changes_is_a_noop():
    state = {}
    list(sync_boards(FakeTrello({BOARD_ID: _board_config()}), state, [BOARD_ID]))
    rows = list(sync_boards(FakeTrello({BOARD_ID: _board_config()}), state, [BOARD_ID]))
    assert rows == []


def test_new_card_caught_by_sweep_even_without_actions():
    state = {}
    list(sync_boards(FakeTrello({BOARD_ID: _board_config()}), state, [BOARD_ID]))

    board = _board_config(
        open_cards=["c1", "c2", "c3"],
        cards={
            **_board_config()["cards"],
            "c3": {
                "id": "c3",
                "name": "Late arrival",
                "desc": "",
                "shortUrl": "https://trello.com/c/c3",
                "idList": "l1",
            },
        },
    )
    rows = list(sync_boards(FakeTrello({BOARD_ID: board}), state, [BOARD_ID]))

    # c3 was added outside the actions window; the sweep still ingests it.
    assert _ids(rows) == ["c3"]
    assert rows[0]["title"] == "Late arrival"


def test_board_document_reemitted_when_structure_changes():
    state = {}
    list(sync_boards(FakeTrello({BOARD_ID: _board_config()}), state, [BOARD_ID]))

    board = _board_config(
        labels=[{"name": "bug", "color": "red"}, {"name": "urgent", "color": "green"}],
        # A board-level action (no card affected) still advances the cursor.
        actions_after=[
            {
                "id": "act3",
                "date": "2026-10-02T09:00:00.000Z",
                "type": "updateBoard",
                "data": {"board": {"id": BOARD_ID}},
            }
        ],
    )
    rows = list(sync_boards(FakeTrello({BOARD_ID: board}), state, [BOARD_ID]))

    assert _ids(rows) == [BOARD_ID]  # only the overview document
    assert "- urgent (green)" in rows[0]["content"]
    assert state["boards"][BOARD_ID]["last_action_id"] == "act3"


# ---------------------------------------------------------------------------
# Forget-on-delete
# ---------------------------------------------------------------------------
def test_vanished_card_emits_hard_delete_marker():
    state = {}
    list(sync_boards(FakeTrello({BOARD_ID: _board_config()}), state, [BOARD_ID]))

    board = _board_config(open_cards=["c1"])  # c2 deleted (or archived) upstream
    rows = list(sync_boards(FakeTrello({BOARD_ID: board}), state, [BOARD_ID]))

    assert rows == [{"id": "c2", "_deleted": True}]
    assert "c2" not in state["cards"]
    assert "c1" in state["cards"]


def test_removed_board_tombstones_its_documents():
    other = _board_config(
        board={"id": OTHER_BOARD_ID, "name": "Ops", "desc": "", "url": "https://trello.com/b/b2"},
        open_cards=["c9"],
        cards={
            "c9": {
                "id": "c9",
                "name": "Ops card",
                "desc": "",
                "shortUrl": "https://trello.com/c/c9",
                "idList": "l1",
            }
        },
        comments={},
        checklists={},
        actions_initial=[
            {
                "id": "act9",
                "date": "2026-10-01T10:00:00.000Z",
                "type": "createCard",
                "data": {"card": {"id": "c9"}},
            }
        ],
    )
    state = {}
    list(
        sync_boards(
            FakeTrello({BOARD_ID: _board_config(), OTHER_BOARD_ID: other}),
            state,
            [BOARD_ID, OTHER_BOARD_ID],
        )
    )

    # OTHER_BOARD_ID dropped from the configuration: its documents are forgotten.
    rows = list(sync_boards(FakeTrello({BOARD_ID: _board_config()}), state, [BOARD_ID]))

    assert rows == [{"id": "c9", "_deleted": True}, {"id": OTHER_BOARD_ID, "_deleted": True}]
    assert set(state["cards"]) == {"c1", "c2"}
    assert set(state["boards"]) == {BOARD_ID}


# ---------------------------------------------------------------------------
# Failure posture
# ---------------------------------------------------------------------------
def test_board_fetch_failure_skips_board_without_deletions():
    other = _board_config(
        board={"id": OTHER_BOARD_ID, "name": "Ops", "desc": "", "url": "https://trello.com/b/b2"},
        open_cards=["x1"],
        cards={
            "x1": {
                "id": "x1",
                "name": "Ops card",
                "desc": "",
                "shortUrl": "https://trello.com/c/x1",
                "idList": "l1",
            }
        },
        comments={},
        checklists={},
        actions_initial=[
            {
                "id": "actx1",
                "date": "2026-10-01T10:00:00.000Z",
                "type": "createCard",
                "data": {"card": {"id": "x1"}},
            }
        ],
    )
    state = {}
    list(
        sync_boards(
            FakeTrello({BOARD_ID: _board_config(), OTHER_BOARD_ID: other}),
            state,
            [BOARD_ID, OTHER_BOARD_ID],
        )
    )
    # OTHER_BOARD_ID now fails its overview fetch (network/auth error): its
    # documents are neither re-emitted nor tombstoned, and its state is kept.
    broken_other = _board_config(board=ValueError("board fetch boom"))
    rows = list(
        sync_boards(
            FakeTrello({BOARD_ID: _board_config(), OTHER_BOARD_ID: broken_other}),
            state,
            [BOARD_ID, OTHER_BOARD_ID],
        )
    )

    assert _ids(rows) == []
    assert state["boards"].get(OTHER_BOARD_ID, {}).get("last_action_id") == "actx1"
    assert any(meta.get("board") == OTHER_BOARD_ID for meta in state["cards"].values())


def test_card_fetch_failure_is_retried_not_deleted():
    state = {}
    list(sync_boards(FakeTrello({BOARD_ID: _board_config()}), state, [BOARD_ID]))

    broken_card = _board_config(
        actions_after=[
            {
                "id": "act3",
                "date": "2026-10-02T09:00:00.000Z",
                "type": "commentCard",
                "data": {"card": {"id": "c1"}},
            }
        ],
    )
    broken_card["cards"]["c1"] = ValueError("card fetch boom")
    rows = list(sync_boards(FakeTrello({BOARD_ID: broken_card}), state, [BOARD_ID]))

    assert rows == []  # skipped, not tombstoned, not half-emitted
    assert "c1" in state["cards"]  # prior state retained; retried next run


# ---------------------------------------------------------------------------
# trello_source — dlt wiring — requires dlt
# ---------------------------------------------------------------------------
def test_trello_source_resource_is_configured_for_merge_and_hard_delete():
    pytest.importorskip("dlt")

    resource = trello_source([BOARD_ID], session=FakeTrello({BOARD_ID: _board_config()}))
    assert resource.name == "trello_documents"

    schema = resource.compute_table_schema()
    write_disposition = schema.get("write_disposition")
    if isinstance(write_disposition, dict):  # dlt may normalize to a config dict
        write_disposition = write_disposition.get("disposition")
    assert write_disposition == "merge"

    columns = schema["columns"]
    assert columns["id"].get("primary_key") is True
    assert columns["_deleted"].get("hard_delete") is True


def test_trello_source_declares_document_marker():
    pytest.importorskip("dlt")
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    resource = trello_source([BOARD_ID], session=FakeTrello({BOARD_ID: _board_config()}))
    # resolve_dlt_sources routes on this marker (not the name); keep it stable.
    assert TRELLO_SOURCE_NAME == "trello"
    assert document_source_tag(resource) == "trello"


def test_trello_source_requires_credentials_or_session():
    pytest.importorskip("dlt")
    with pytest.raises(ValueError, match="api_key and token"):
        trello_source([BOARD_ID])


def test_trello_source_requires_dlt(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "dlt":
            raise ImportError("no dlt")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError, match="cognee-community-connector-trello"):
        trello_source([BOARD_ID], session=FakeTrello({BOARD_ID: _board_config()}))


def test_trello_source_validates_board_ids():
    pytest.importorskip("dlt")
    with pytest.raises(ValueError, match="board_ids"):
        trello_source([], session=FakeTrello({}))
    with pytest.raises(ValueError, match="board_ids"):
        trello_source(["b1", ""], session=FakeTrello({}))


# ---------------------------------------------------------------------------
# End-to-end: a real dlt merge acts on the hard-delete marker, and the
# incremental state persists across pipeline runs
# ---------------------------------------------------------------------------
def test_forget_on_delete_and_incremental_end_to_end_through_a_real_dlt_pipeline(tmp_path):
    dlt = pytest.importorskip("dlt")

    db_path = (tmp_path / "trello.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="test_trello_e2e",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="boards",
        pipelines_dir=str(tmp_path / "state"),
    )

    # Sync #1: the board document and two cards land in the destination.
    pipeline.run(trello_source([BOARD_ID], session=FakeTrello({BOARD_ID: _board_config()})))
    with pipeline.sql_client() as client:
        assert client.execute_sql("SELECT count(*) FROM trello_documents")[0][0] == 3

    # Sync #2 (same pipeline → persisted dlt state): c2 is deleted upstream and
    # c1 gets a new comment. The connector emits a hard-delete marker for c2
    # and an updated document for c1; the merge applies both.
    board = _board_config(
        open_cards=["c1"],
        actions_after=[
            {
                "id": "act3",
                "date": "2026-10-02T09:00:00.000Z",
                "type": "commentCard",
                "data": {"card": {"id": "c1"}},
            }
        ],
        comments={
            "c1": [
                {"data": {"text": "First comment"}, "memberCreator": {"username": "alice"}},
                {"data": {"text": "Second comment"}, "memberCreator": {"username": "bob"}},
            ]
        },
    )
    pipeline.run(trello_source([BOARD_ID], session=FakeTrello({BOARD_ID: board})))
    with pipeline.sql_client() as client:
        remaining = {row[0] for row in client.execute_sql("SELECT id FROM trello_documents")}
        contents = dict(client.execute_sql("SELECT id, content FROM trello_documents"))

    assert remaining == {BOARD_ID, "c1"}  # c2 forgotten from the destination
    assert "Second comment" in contents["c1"]  # c1's document updated in place
