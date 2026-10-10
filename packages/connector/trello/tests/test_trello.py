"""Unit and dlt-pipeline tests for the Trello connector. No live Trello account needed.

* Client tests run ``TrelloClient`` against ``httpx.MockTransport``.
* Rendering tests cover card and board documents.
* Sync tests drive ``_iter_rows`` with a fake API and a plain dict as state,
  including the action types Trello leaves out of the feed, and run
  ``trello_source`` through a dlt pipeline into a temp sqlite destination.
"""

import copy
from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

import httpx
import pytest
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item
from conftest import BOARD, PRIYA

from cognee_community_connector_trello.trello import (
    TrelloAPIError,
    TrelloAuthError,
    TrelloClient,
    _comments,
    _iter_rows,
    _TrelloConfig,
    render_board,
    render_card,
    trello_source,
)


def _config(**overrides):
    values = {
        "board_ids": (BOARD,),
        "workspace_id": None,
        "include_archived": True,
        "include_comments": True,
        "include_checklists": True,
        "full_resync": False,
    }
    values.update(overrides)
    return _TrelloConfig(**values)


def _stats():
    return {"emitted": 0, "unchanged": 0, "deleted": 0, "gone": 0}


def _run(trello, state, stats=None, **overrides):
    stats = stats if stats is not None else _stats()
    return list(_iter_rows(trello, _config(**overrides), state, stats))


def _ids(rows):
    return sorted(row["id"] for row in rows)


def _comment_calls(trello):
    return [p for path, p in trello.calls if p.get("filter") == "commentCard"]


# ---------------------------------------------------------------------------
# TrelloClient over httpx.MockTransport
# ---------------------------------------------------------------------------
def _client(responses):
    queue = list(responses)
    requests, sleeps = [], []

    def handler(request):
        requests.append(request)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    client = TrelloClient(
        "key-1",
        "tok-1",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleeps.append,
    )
    return client, requests, sleeps


def test_client_sends_credentials_in_the_header_not_the_url():
    client, requests, _ = _client([httpx.Response(200, json={"id": "me"})])
    client.get("/members/me", {"fields": "id"})
    request = requests[0]
    assert request.headers["Authorization"] == (
        'OAuth oauth_consumer_key="key-1", oauth_token="tok-1"'
    )
    assert "tok-1" not in str(request.url)
    assert str(request.url) == "https://api.trello.com/1/members/me?fields=id"


def test_client_retries_429_and_5xx():
    client, _, sleeps = _client(
        [
            httpx.Response(429, json={"error": "API_TOKEN_LIMIT_EXCEEDED"}),
            httpx.Response(503),
            httpx.Response(200, json=[]),
        ]
    )
    assert client.get("/boards/b1/actions") == []
    assert sleeps == [1.0, 2.0]


def test_client_error_carries_status_only():
    client, requests, _ = _client(
        [httpx.Response(404, text="The requested resource was not found.")]
    )
    with pytest.raises(TrelloAPIError) as info:
        client.get("/boards/b1")
    assert info.value.status == 404
    assert "resource" not in str(info.value)
    assert len(requests) == 1


def test_client_rejects_bad_credentials_without_echoing_them():
    with pytest.raises(ValueError) as info:
        TrelloClient("key", "s3cr3t with space")
    assert "s3cr3t" not in str(info.value)
    assert "s3cr3t" not in repr(TrelloClient("key", "s3cr3t"))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def test_render_card_includes_fields_checklists_and_comments(trello):
    card = trello.add_card(
        "c1",
        "Export fails on large boards",
        desc="Happens above 5k rows.",
        labels=[{"name": "bug"}, {"name": "", "color": "green"}],
        idMembers=["m1", "m9"],
        due="2026-10-15T12:00:00.000Z",
        dueComplete=True,
    )
    checklist = {
        "id": "k1",
        "idCard": "c1",
        "name": "QA",
        "pos": 1,
        "checkItems": [
            {"id": "i2", "name": "Release notes", "state": "incomplete", "pos": 2},
            {"id": "i1", "name": "Write tests", "state": "complete", "pos": 1},
        ],
    }
    comment = trello.comment("c1", "Reproduced on staging.")
    row = render_card(card, trello.boards[BOARD], [checklist], [comment])
    assert row["id"] == "card:c1"
    assert row["title"] == "Export fails on large boards"
    assert row["url"] == "https://trello.com/c/c1"
    assert row["content"] == (
        "Board: Product\n"
        "List: To do\n"
        "Labels: bug, green\n"
        "Members: Priya Shah\n"
        "Due: 2026-10-15 (done)\n"
        "\n"
        "Happens above 5k rows.\n"
        "\n"
        "Checklists:\n"
        "QA:\n"
        "- [x] Write tests\n"
        "- [ ] Release notes\n"
        "\n"
        "Comments:\n"
        "Sam Lee (2026-10-05): Reproduced on staging."
    )


def test_card_in_an_archived_list_counts_as_archived(trello):
    card = trello.add_card("c1", "Old idea", idList="l2")
    trello.boards[BOARD]["lists"][1]["closed"] = True
    assert "Status: archived" in render_card(card, trello.boards[BOARD], [], [])["content"]


def test_render_board_lists_structure_without_card_titles(trello):
    trello.add_card("c1", "Secret card title")
    trello.boards[BOARD]["lists"][1]["closed"] = True
    row = render_board(trello.boards[BOARD])
    assert row["id"] == f"board:{BOARD}"
    assert row["content"] == (
        "Board: Product\n\nRoadmap and bugs\n\nLists:\n- To do\n- Done (archived)\n"
        "\nLabels: bug\nMembers: Priya Shah, Sam Lee"
    )
    assert "Secret card title" not in row["content"]


def test_row_becomes_a_trello_document(trello):
    row = render_card(trello.add_card("c1", "Export"), trello.boards[BOARD], [], [])
    dlt_row = SimpleNamespace(
        table_name="trello_cards", primary_key_value=row["id"], row_data=row, content_hash="h"
    )
    item = _build_document_data_item(dlt_row, uuid5(NAMESPACE_OID, row["id"]), "trello")
    assert item.data.startswith("# Export")
    assert item.system_metadata["source"] == "trello"


# ---------------------------------------------------------------------------
# Sync state machine (_iter_rows with a dict as state)
# ---------------------------------------------------------------------------
def test_first_sync_emits_the_board_and_every_card(trello):
    trello.add_card("c1", "Export")
    trello.add_card("c2", "Dark mode")
    trello.comment("c1", "On it")
    rows = _run(trello, {})
    assert _ids(rows) == [f"board:{BOARD}", "card:c1", "card:c2"]
    assert "Sam Lee (2026-10-05): On it" in next(r for r in rows if r["id"] == "card:c1")["content"]


def test_no_change_sync_emits_nothing_and_skips_comments(trello):
    trello.add_card("c1", "Export")
    state = {}
    _run(trello, state)
    trello.calls.clear()

    assert _run(trello, state) == []
    assert _comment_calls(trello) == []


def test_feed_action_reprocesses_only_the_changed_card(trello):
    trello.add_card("c1", "Export")
    trello.add_card("c2", "Dark mode")
    state = {}
    _run(trello, state)

    trello.card("c1")["desc"] = "Fails above 5k rows."
    trello.act(BOARD, card={"id": "c1"})
    rows = _run(trello, state)
    assert _ids(rows) == ["card:c1"]
    assert "Fails above 5k rows." in rows[0]["content"]


def test_new_comment_reaches_its_card(trello):
    trello.add_card("c1", "Export")
    state = {}
    _run(trello, state)

    trello.comment("c1", "Fixed in 2.3", author=PRIYA)
    rows = _run(trello, state)
    assert _ids(rows) == ["card:c1"]
    assert "Priya Shah (2026-10-05): Fixed in 2.3" in rows[0]["content"]


def test_checklist_change_without_any_action_is_caught_by_the_snapshot(trello):
    trello.add_card("c1", "Export")
    trello.comment("c1", "Keep me")
    item = {"id": "i1", "name": "Write tests", "state": "incomplete", "pos": 1}
    trello.boards[BOARD]["checklists"] = [
        {"id": "k1", "idCard": "c1", "name": "QA", "pos": 1, "checkItems": [item]}
    ]
    state = {}
    _run(trello, state)

    # updateCheckItem is excluded from the feed; here even activity stays put.
    item["name"] = "Write integration tests"
    rows = _run(trello, state)
    assert _ids(rows) == ["card:c1"]
    assert "- [ ] Write integration tests" in rows[0]["content"]
    # The card is re-rendered whole, so its comments must survive.
    assert "Sam Lee (2026-10-05): Keep me" in rows[0]["content"]


def test_silent_comment_edit_is_caught_by_full_resync(trello):
    trello.add_card("c1", "Export")
    comment = trello.comment("c1", "v1")
    state = {}
    _run(trello, state)

    # updateComment is excluded from the feed; worst case, activity stays put too.
    comment["data"]["text"] = "v2"
    assert _run(trello, state) == []
    rows = _run(trello, state, full_resync=True)
    assert "v2" in rows[0]["content"]


def test_deleted_card_is_forgotten(trello):
    trello.add_card("c1", "Export")
    trello.add_card("c2", "Dark mode")
    state = {}
    _run(trello, state)

    trello.boards[BOARD]["cards"] = [trello.card("c1")]
    trello.act(BOARD, "deleteCard", card={"id": "c2"})
    assert _run(trello, state) == [{"id": "card:c2", "_deleted": True}]


def test_archived_cards_are_kept_or_forgotten(trello):
    trello.add_card("c1", "Export")
    state = {}
    _run(trello, state)
    trello.card("c1")["closed"] = True
    trello.act(BOARD, card={"id": "c1"})

    kept = _run(trello, copy.deepcopy(state))
    assert "Status: archived" in kept[0]["content"]
    assert _run(trello, state, include_archived=False) == [{"id": "card:c1", "_deleted": True}]


@pytest.mark.parametrize("status", [404, 401])
def test_gone_board_forgets_the_board_and_its_cards(trello, status):
    trello.add_card("c1", "Export")
    state = {}
    _run(trello, state)

    trello.errors[BOARD] = status
    stats = _stats()
    rows = _run(trello, state, stats=stats)
    assert _ids(rows) == [f"board:{BOARD}", "card:c1"]
    assert all(row["_deleted"] for row in rows)
    assert stats["gone"] == 1


def test_deselected_board_is_forgotten(trello):
    trello.add_board("b2", "Ops")
    trello.add_card("c1", "Export")
    state = {}
    _run(trello, state)
    rows = _run(trello, state, board_ids=("b2",))
    assert {r["id"] for r in rows if r.get("_deleted")} == {f"board:{BOARD}", "card:c1"}


def test_closed_board_is_forgotten_when_archived_is_excluded(trello):
    trello.add_card("c1", "Export")
    state = {}
    _run(trello, state)
    trello.boards[BOARD]["closed"] = True
    rows = _run(trello, state, include_archived=False)
    assert _ids(rows) == [f"board:{BOARD}", "card:c1"]


def test_bad_token_raises_and_forgets_nothing(trello):
    trello.add_card("c1", "Export")
    state = {}
    _run(trello, state)
    trello.bad_token = True
    with pytest.raises(TrelloAuthError):
        _run(trello, state)
    assert state["boards"][BOARD]["cards"]


def test_other_errors_raise_instead_of_forgetting(trello):
    trello.add_card("c1", "Export")
    state = {}
    _run(trello, state)
    trello.errors[BOARD] = 500
    with pytest.raises(TrelloAPIError):
        _run(trello, state)


def test_boards_come_from_the_workspace_or_the_member(trello):
    trello.add_board("b2", "Ops", organization="acme")
    trello.add_board("b3", "Old", organization="acme", closed=True)
    rows = _run(trello, {}, board_ids=(), workspace_id="acme", include_archived=False)
    assert {r["id"] for r in rows} == {"board:b2"}
    rows = _run(trello, {}, board_ids=())
    assert {r["id"] for r in rows} == {f"board:{BOARD}", "board:b2", "board:b3"}


def test_comments_page_backwards_past_1000(trello):
    trello.add_card("c1", "Export")
    for index in range(1005):
        trello.comment("c1", f"comment {index}")
    by_card = _comments(trello, BOARD)
    assert len(by_card["c1"]) == 1005
    assert len(_comment_calls(trello)) == 2


# ---------------------------------------------------------------------------
# trello_source factory
# ---------------------------------------------------------------------------
def test_source_declares_document_path_and_scope(trello):
    source = trello_source([BOARD], client=trello, resource_name="trello_team_a")
    assert getattr(source, dlt_utils.DOCUMENT_SOURCE_ATTR) == "trello"
    assert getattr(source, dlt_utils.PIPELINE_SCOPE_ATTR) == "trello_team_a"
    assert source.name == "trello_team_a"


def test_missing_credentials_name_the_env_vars(monkeypatch):
    monkeypatch.delenv("TRELLO_API_KEY", raising=False)
    monkeypatch.delenv("TRELLO_TOKEN", raising=False)
    with pytest.raises(ValueError, match="TRELLO_TOKEN"):
        trello_source([BOARD], api_key="key")


# ---------------------------------------------------------------------------
# dlt pipeline: incremental cursor + forget-on-delete
# ---------------------------------------------------------------------------
@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def _pipeline(dlt, tmp_path):
    db_path = (tmp_path / "trello.db").as_posix()
    return dlt.pipeline(
        pipeline_name="trello_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="trello_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def _staged(pipeline):
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, content FROM trello_cards") as cursor,
    ):
        return {row[0]: row[1] for row in cursor.fetchall()}


def test_pipeline_resyncs_only_changes_and_forgets_deletions(dlt_mod, tmp_path, trello):
    trello.add_card("c1", "Export")
    trello.add_card("c2", "Dark mode")
    pipeline = _pipeline(dlt_mod, tmp_path)

    pipeline.run(trello_source([BOARD], client=trello))
    assert set(_staged(pipeline)) == {f"board:{BOARD}", "card:c1", "card:c2"}

    # The cursor lives in dlt state, so a fresh source object resumes from it.
    source = trello_source([BOARD], client=trello)
    pipeline.run(source)
    assert source.cognee_sync_stats["emitted"] == 0

    trello.boards[BOARD]["cards"] = [trello.card("c1")]
    trello.act(BOARD, "deleteCard", card={"id": "c2"})
    pipeline.run(trello_source([BOARD], client=trello))
    assert set(_staged(pipeline)) == {f"board:{BOARD}", "card:c1"}


def test_failed_run_keeps_staging_and_cursor(dlt_mod, tmp_path, trello):
    trello.add_card("c1", "Export")
    pipeline = _pipeline(dlt_mod, tmp_path)
    pipeline.run(trello_source([BOARD], client=trello))

    trello.boards[BOARD]["cards"] = []
    trello.errors[BOARD] = 500
    with pytest.raises(Exception, match="500"):
        pipeline.run(trello_source([BOARD], client=trello))
    assert set(_staged(pipeline)) == {f"board:{BOARD}", "card:c1"}

    trello.errors.clear()
    pipeline.run(trello_source([BOARD], client=trello))
    assert set(_staged(pipeline)) == {f"board:{BOARD}"}
