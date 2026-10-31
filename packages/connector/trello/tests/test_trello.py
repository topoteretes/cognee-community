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
from dlt.pipeline.exceptions import PipelineStepFailed

from cognee_community_connector_trello.trello import (
    TrelloAPIError,
    TrelloAuthError,
    TrelloClient,
    TrelloRateLimitedError,
    _iter_rows,
    _TrelloConfig,
    _Walker,
    render_board,
    render_card,
    trello_source,
)

NODE_SET = dlt_utils.NODE_SET_COLUMN


def _config(**overrides):
    values = {
        "board_ids": (BOARD,),
        "workspace_id": None,
        "include_archived": False,
        "include_comments": True,
        "include_checklists": True,
        "full_resync": False,
    }
    values.update(overrides)
    return _TrelloConfig(**values)


def _stats():
    return {"emitted": 0, "unchanged": 0, "deleted": 0, "gone": 0, "failed": 0}


def _run(trello, state, stats=None, **overrides):
    stats = stats if stats is not None else _stats()
    return list(_iter_rows(trello, _config(**overrides), state, stats))


def _ids(rows):
    return sorted(row["id"] for row in rows)


def _comment_calls(trello):
    return [path for path, p in trello.calls if p.get("filter") == "commentCard"]


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


def test_client_honours_retry_after_and_retries_5xx():
    client, _, sleeps = _client(
        [
            httpx.Response(429, headers={"Retry-After": "60"}),
            httpx.Response(503),
            httpx.Response(200, json=[]),
        ]
    )
    assert client.get("/boards/b1/actions") == []
    assert sleeps == [60.0, 2.0]


def test_client_stops_on_a_long_retry_after_or_endless_429():
    client, requests, _ = _client([httpx.Response(429, headers={"Retry-After": "600"})])
    with pytest.raises(TrelloRateLimitedError):
        client.get("/boards/b1")
    assert len(requests) == 1

    client, requests, _ = _client([httpx.Response(429)])
    with pytest.raises(TrelloRateLimitedError):
        client.get("/boards/b1")
    assert len(requests) == 5


def test_client_checks_access_around_every_retry_wait():
    checks = []

    def revoke_after_the_first_check():
        checks.append(True)
        if len(checks) > 1:
            raise PermissionError("access revoked")

    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(503)

    client = TrelloClient(
        "key-1",
        "tok-1",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda seconds: None,
        check_active=revoke_after_the_first_check,
    )
    with pytest.raises(PermissionError):
        client.get("/members/me")
    # Checked before the wait and again after it, so the retry never goes out.
    assert len(requests) == 1 and len(checks) == 2


def test_client_closes_only_the_http_client_it_made():
    injected = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    TrelloClient("key-1", "tok-1", http=injected).close()
    assert not injected.is_closed

    owned = TrelloClient("key-1", "tok-1")
    owned._http = made = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    )
    owned._owned = True
    owned.close()
    assert made.is_closed and owned._http is None


def test_client_errors_carry_status_only():
    client, requests, _ = _client([httpx.Response(401, text="invalid token")])
    with pytest.raises(TrelloAuthError):
        client.get("/members/me")
    not_found = httpx.Response(404, text="The requested resource was not found.")
    client, requests, _ = _client([not_found])
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
    assert row[NODE_SET] == [f"trello:{BOARD}"]
    assert row["content"] == (
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


def test_api_ordering_never_changes_a_row(trello):
    card = trello.add_card(
        "c1", "Export", labels=[{"name": "ui"}, {"name": "bug"}], idMembers=["m2", "m1"]
    )
    items = [
        {"id": "i1", "name": "a", "state": "complete"},
        {"id": "i2", "name": "b", "state": "complete"},
    ]
    checklists = [{"id": "k1", "name": "QA", "checkItems": items}]
    board = trello.boards[BOARD]
    before = render_card(card, board, checklists, [])

    shuffled = copy.deepcopy(board)
    shuffled["lists"].reverse()
    card["labels"].reverse()
    card["idMembers"].reverse()
    items.reverse()
    assert render_card(card, shuffled, checklists, []) == before
    assert render_board(shuffled) == render_board(board)


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


def test_row_becomes_a_trello_document_in_its_board_node_set(trello):
    row = render_card(trello.add_card("c1", "Export"), trello.boards[BOARD], [], [])
    dlt_row = SimpleNamespace(
        table_name="trello_b1", primary_key_value=row["id"], row_data=row, content_hash="h"
    )
    item = _build_document_data_item(dlt_row, uuid5(NAMESPACE_OID, row["id"]), "trello")
    assert item.data.startswith("# Export")
    assert item.system_metadata["source"] == "trello"
    assert item.node_set == [f"trello:{BOARD}"]


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


def test_no_change_sync_costs_three_requests(trello):
    trello.add_card("c1", "Export")
    trello.comment("c1", "On it")
    state = {}
    _run(trello, state)
    trello.calls.clear()

    assert _run(trello, state) == []
    assert [path for path, _ in trello.calls] == [
        "/members/me",
        f"/boards/{BOARD}/actions",
        f"/boards/{BOARD}",
    ]


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


def test_cursor_is_read_before_the_board_and_asks_for_every_action_type(trello):
    trello.add_card("c1", "Export")
    _run(trello, {})
    paths = [path for path, _ in trello.calls]
    assert paths.index(f"/boards/{BOARD}/actions") < paths.index(f"/boards/{BOARD}")
    feed = next(p for path, p in trello.calls if path.endswith("/actions"))
    assert feed["filter"] == "all"


def test_new_comment_is_read_for_that_card_only(trello):
    trello.add_card("c1", "Export")
    trello.add_card("c2", "Dark mode")
    trello.comment("c2", "Later")
    state = {}
    _run(trello, state)
    trello.calls.clear()

    trello.comment("c1", "Fixed in 2.3", author=PRIYA)
    rows = _run(trello, state)
    assert _ids(rows) == ["card:c1"]
    assert "Priya Shah (2026-10-05): Fixed in 2.3" in rows[0]["content"]
    assert _comment_calls(trello) == ["/cards/c1/actions"]
    params = next(p for path, p in trello.calls if path == "/cards/c1/actions")
    assert params["memberCreator_fields"] == "fullName" and params["member"] == "false"


def test_deleted_comment_is_caught_through_the_comment_count(trello):
    trello.add_card("c1", "Export")
    trello.comment("c1", "keep")
    gone = trello.comment("c1", "drop")
    state = {}
    _run(trello, state)

    # deleteComment is excluded from the feed; here even activity stays put.
    trello.actions[BOARD].remove(gone)
    trello.boards[BOARD]["dateLastActivity"] = "2026-10-06T00:00:00.000Z"
    rows = _run(trello, state)
    assert "drop" not in rows[0]["content"] and "keep" in rows[0]["content"]


def test_many_changed_cards_read_comments_for_the_whole_board(trello):
    for index in range(5):
        trello.add_card(f"c{index}", f"Card {index}")
        trello.comment(f"c{index}", "note")
    _run(trello, {})
    assert _comment_calls(trello) == [f"/boards/{BOARD}/actions"]


def test_card_over_1000_comments_is_read_from_the_board_feed(trello):
    trello.add_card("c1", "Export")
    for index in range(1050):
        trello.comment("c1", f"comment-{index:04d}")
    rows = _run(trello, {})
    card = next(row for row in rows if row["id"] == "card:c1")
    assert "comment-0000" in card["content"] and card["content"].count("comment-") == 1050
    assert _comment_calls(trello) == [f"/boards/{BOARD}/actions"] * 2


def test_card_with_exactly_1000_comments_is_read_from_the_board_feed(trello):
    trello.add_card("c1", "Export")
    for index in range(1000):
        trello.comment("c1", f"comment-{index:04d}")
    rows = _run(trello, {})
    card = next(row for row in rows if row["id"] == "card:c1")
    assert card["content"].count("comment-") == 1000
    assert all(not path.startswith("/cards/") for path in _comment_calls(trello))


def test_card_endpoint_stopping_at_1000_comments_raises_naming_the_card(trello):
    trello.add_card("c1", "Export")
    for index in range(1001):
        trello.comment("c1", f"comment {index}")
    walker = _Walker(trello, _config(), {}, _stats(), None)
    with pytest.raises(TrelloAPIError, match="card c1 has more comments"):
        walker._card_comments("c1")


@pytest.mark.parametrize("board_ids", [(BOARD,), ()])
def test_card_deleted_during_the_run_keeps_its_board(trello, board_ids):
    trello.add_card("c1", "Export")
    trello.add_card("c2", "Dark mode")
    state = {}
    _run(trello, state, board_ids=board_ids)

    trello.comment("c1", "New note")
    trello.errors["c1"] = 404
    stats = _stats()
    assert _run(trello, state, stats=stats, board_ids=board_ids) == []
    assert stats["gone"] == 0 and set(state["boards"][BOARD]["cards"]) == {"c1", "c2"}

    # The board cursor did not move, so the next run reads the card again.
    del trello.errors["c1"]
    rows = _run(trello, state, board_ids=board_ids)
    assert _ids(rows) == ["card:c1"] and "New note" in rows[0]["content"]


def test_checklist_change_without_any_action_is_caught_and_keeps_comments(trello):
    trello.add_card("c1", "Export")
    trello.comment("c1", "Keep me")
    item = {"id": "i1", "name": "Write tests", "state": "incomplete", "pos": 1}
    trello.boards[BOARD]["checklists"] = [
        {"id": "k1", "idCard": "c1", "name": "QA", "pos": 1, "checkItems": [item]}
    ]
    state = {}
    _run(trello, state)

    # updateCheckItem is excluded from the feed but moves dateLastActivity.
    item["name"] = "Write integration tests"
    trello.boards[BOARD]["dateLastActivity"] = "2026-10-06T00:00:00.000Z"
    rows = _run(trello, state)
    assert _ids(rows) == ["card:c1"]
    assert "- [ ] Write integration tests" in rows[0]["content"]
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


@pytest.mark.parametrize("flag", ["include_comments", "include_checklists"])
def test_changing_an_include_flag_rerenders_the_cards(trello, flag):
    trello.add_card("c1", "Export")
    trello.comment("c1", "note")
    trello.boards[BOARD]["checklists"] = [
        {"id": "k1", "idCard": "c1", "name": "QA", "checkItems": [{"id": "i", "name": "x"}]}
    ]
    state = {}
    _run(trello, state)

    off = _run(trello, state, **{flag: False})
    assert _ids(off) == ["card:c1"]
    on = _run(trello, state)
    assert _ids(on) == ["card:c1"]
    assert "note" in on[0]["content"] and "QA:" in on[0]["content"]


def test_board_rename_reprocesses_only_the_board(trello):
    trello.add_card("c1", "Export")
    state = {}
    _run(trello, state)
    trello.boards[BOARD]["name"] = "Product 2027"
    trello.act(BOARD, "updateBoard", board={"id": BOARD})
    assert _ids(_run(trello, state)) == [f"board:{BOARD}"]


def test_deleted_card_is_forgotten(trello):
    trello.add_card("c1", "Export")
    trello.add_card("c2", "Dark mode")
    state = {}
    _run(trello, state)

    trello.boards[BOARD]["cards"] = [trello.card("c1")]
    trello.act(BOARD, "deleteCard", card={"id": "c2"})
    assert _run(trello, state) == [{"id": "card:c2", "_deleted": True}]


@pytest.mark.parametrize("order", [("b1", "b2"), ("b2", "b1")])
def test_card_moved_between_selected_boards_survives(trello, order):
    trello.add_board("b2", "Ops")
    trello.add_card("c1", "Export")
    state = {}
    _run(trello, state, board_ids=order)

    trello.move_card("c1", "b1", "b2")
    rows = _run(trello, state, board_ids=order)
    assert {"id": "card:c1", "_deleted": True} not in rows
    moved = next(r for r in rows if r["id"] == "card:c1")
    assert moved[NODE_SET] == ["trello:b2"]
    assert _run(trello, state, board_ids=order) == []


def test_archived_cards_are_left_out_by_default(trello):
    trello.add_card("c1", "Export")
    state = {}
    _run(trello, state, include_archived=True)
    trello.card("c1")["closed"] = True
    trello.act(BOARD, card={"id": "c1"})

    kept = _run(trello, copy.deepcopy(state), include_archived=True)
    assert "Status: archived" in kept[0]["content"]
    rows = _run(trello, state)
    assert {"id": "card:c1", "_deleted": True} in rows


@pytest.mark.parametrize("status", [404, 401])
def test_unreadable_configured_board_raises_and_forgets_nothing(trello, status):
    trello.add_card("c1", "Export")
    state = {}
    _run(trello, state)

    trello.errors[BOARD] = status
    with pytest.raises((TrelloAPIError, TrelloAuthError)):
        _run(trello, state)
    assert state["boards"][BOARD]["cards"]


def test_discovered_board_that_answers_404_is_forgotten(trello):
    trello.add_card("c1", "Export")
    state = {}
    _run(trello, state, board_ids=())

    trello.errors[BOARD] = 404
    stats = _stats()
    rows = _run(trello, state, stats=stats, board_ids=())
    assert _ids(rows) == [f"board:{BOARD}", "card:c1"]
    assert all(row["_deleted"] for row in rows) and stats["gone"] == 1


def test_expired_token_after_progress_stops_without_forgetting(trello):
    trello.add_board("b2", "Ops")
    trello.add_card("c1", "Export")
    trello.add_card("c2", "Deploy", board_id="b2")
    state = {}
    _run(trello, state, board_ids=("b1", "b2"))

    trello.card("c1")["desc"] = "new"
    trello.act(BOARD, card={"id": "c1"})
    trello.errors["b2"] = 401
    stats = _stats()
    rows = _run(trello, state, stats=stats, board_ids=("b1", "b2"))
    assert _ids(rows) == ["card:c1"]
    assert stats["failed"] == 1 and stats["failed_auth"] == 1
    assert state["boards"]["b2"]["cards"]


def test_rate_limit_stops_cleanly_and_skips_deletions(trello):
    trello.add_board("b2", "Ops")
    trello.add_card("c2", "Deploy", board_id="b2")
    state = {}
    _run(trello, state, board_ids=("b1", "b2"))

    trello.rate_limited.add("b2")
    stats = _stats()
    assert _run(trello, state, stats=stats, board_ids=("b1", "b2")) == []
    assert stats["failed_rate_limit"] == 1
    assert state["boards"]["b2"]["cards"]


def test_request_budget_is_checked_after_the_first_saved_board(trello):
    trello.add_board("b2", "Ops")
    trello.add_card("c2", "Deploy", board_id="b2")
    stats = _stats()
    rows = _run(trello, {}, stats=stats, board_ids=("b1", "b2"), max_requests=3)
    assert _ids(rows) == [f"board:{BOARD}"]
    assert stats["failed_budget"] == 1


def test_request_budget_stops_inside_a_board_and_resumes(trello):
    for index in range(3):
        trello.add_card(f"c{index}", f"Card {index}")
        for note in range(700):
            trello.comment(f"c{index}", f"note {note}")
    state = {}
    emitted, paused = [], []
    for _ in range(4):
        stats = _stats()
        emitted.append(_ids(_run(trello, state, stats=stats, max_requests=10)))
        paused.append(stats.get("failed_budget", 0))
        # A cut run is incomplete, so failed is set with the budget as the reason.
        assert stats["failed"] == paused[-1]
    assert emitted == [[f"board:{BOARD}", "card:c0"], ["card:c1"], ["card:c2"], []]
    assert paused == [1, 1, 0, 0]


def test_deleted_card_is_forgotten_even_when_every_run_is_cut_short(trello):
    trello.add_board("b2", "Ops")
    trello.add_card("c1", "Export")
    trello.add_card("c2", "Deploy", board_id="b2")
    state = {}
    _run(trello, state, board_ids=("b1", "b2"))

    trello.boards[BOARD]["cards"].clear()
    trello.act(BOARD, "deleteCard", card={"id": "c1"})
    trello.act("b2", "updateCard", card={"id": "c2"})
    # Each run finishes one board before the budget stops it; the pass spans both runs,
    # and the second is cut again before it can confirm c1, which stays an orphan.
    first = _run(trello, state, board_ids=("b1", "b2"), max_requests=5)
    assert {"id": "card:c1", "_deleted": True} not in first
    second = _run(trello, state, board_ids=("b1", "b2"), max_requests=5)
    assert second == [] and state["pass"]["orphans"] == ["c1"]
    third = _run(trello, state, board_ids=("b1", "b2"), max_requests=5)
    assert {"id": "card:c1", "_deleted": True} in third
    assert state["pass"] == {"present": {}, "orphans": []}


@pytest.mark.parametrize("archive_old_board", [False, True])
def test_card_moved_to_a_board_the_pass_already_read_survives(trello, archive_old_board):
    trello.add_board("b2", "Ops")
    trello.add_card("c1", "Export", board_id="b2")
    ids = ("b1", "b2")
    state = {}
    _run(trello, state, board_ids=ids)

    # The budget stops the run after b1, then c1 moves from b2 to b1.
    stats = _stats()
    _run(trello, state, stats=stats, board_ids=ids, max_requests=3)
    assert stats["failed_budget"] == 1 and list(state["pass"]["present"]) == [BOARD]
    trello.move_card("c1", "b2", BOARD)
    if archive_old_board:
        trello.boards["b2"]["closed"] = True

    # The pass completes on b2 without c1 in either listing; Trello says it is on b1.
    rows = _run(trello, state, board_ids=ids)
    assert {"id": "card:c1", "_deleted": True} not in rows
    moved = next(r for r in rows if r["id"] == "card:c1")
    assert moved[NODE_SET] == [f"trello:{BOARD}"]
    assert _run(trello, state, board_ids=ids) == []


def test_card_in_a_list_archived_after_the_listing_is_forgotten(trello):
    trello.add_card("c1", "Export")
    state = {}
    _run(trello, state)
    trello.boards[BOARD]["lists"][0]["closed"] = True
    trello.act(BOARD, "updateList", list={"id": "l1"})
    assert {"id": "card:c1", "_deleted": True} in _run(trello, state)


def test_card_out_of_reach_is_forgotten_but_an_expired_token_forgets_nothing(trello):
    trello.add_board("b2", "Ops")
    trello.add_card("c1", "Export")
    trello.add_card("c2", "Deploy")
    state = {}
    _run(trello, state, board_ids=("b1", "b2"))
    # c1 moved to a board this token cannot read, which answers 401 for the card.
    trello.boards[BOARD]["cards"] = [trello.card("c2")]
    trello.act(BOARD, "moveCardFromBoard", card={"id": "c1"})
    trello.errors["c1"] = 401

    expiring = copy.deepcopy(state)
    real_get = trello.get
    checks = []

    def token_expires_after_the_first_check(path, params=None):
        if path == "/members/me":
            checks.append(path)
            if len(checks) > 1:
                raise TrelloAuthError("Trello rejected the API key or token (HTTP 401)")
        return real_get(path, params)

    trello.get = token_expires_after_the_first_check
    with pytest.raises(TrelloAuthError):
        _run(trello, expiring, board_ids=("b1", "b2"))
    assert expiring["pass"]["orphans"] == ["c1"]

    trello.get = real_get
    assert {"id": "card:c1", "_deleted": True} in _run(trello, state, board_ids=("b1", "b2"))


def test_card_moved_off_a_removed_board_survives(trello):
    trello.add_board("b2", "Ops")
    trello.add_card("c1", "Export")
    state = {}
    _run(trello, state, board_ids=("b1", "b2"))

    trello.move_card("c1", BOARD, "b2")
    rows = _run(trello, state, board_ids=("b2",))
    assert {"id": f"board:{BOARD}", "_deleted": True} in rows
    assert {"id": "card:c1", "_deleted": True} not in rows
    assert "c1" in state["boards"]["b2"]["cards"]


def test_empty_board_listing_waits_for_confirmation(trello):
    trello.add_card("c1", "Export")
    state = {}
    _run(trello, state, board_ids=())
    trello.boards.clear()
    assert _run(trello, state, board_ids=()) == []
    assert BOARD in state["boards"]
    assert _ids(_run(trello, state, board_ids=())) == [f"board:{BOARD}", "card:c1"]
    assert "empty_listing" not in state


def test_new_board_that_answers_404_before_its_first_save_is_skipped(trello):
    trello.add_board("b2", "Ops")
    trello.add_card("c1", "Export")
    trello.errors["b2"] = 404
    state = {}
    rows = _run(trello, state, board_ids=())
    assert _ids(rows) == [f"board:{BOARD}", "card:c1"]
    assert "b2" not in state["boards"]


def test_board_answer_without_a_card_list_raises_and_forgets_nothing(trello):
    trello.add_card("c1", "Export")
    state = {}
    _run(trello, state)
    real_get = trello.get
    trello.get = lambda path, params=None: (
        {"id": BOARD} if params and "cards" in params else real_get(path, params)
    )
    trello.act(BOARD)
    with pytest.raises(TrelloAPIError, match="card list"):
        _run(trello, state)
    assert "c1" in state["boards"][BOARD]["cards"]


def test_board_listing_that_is_not_a_list_raises(trello):
    trello.add_card("c1", "Export")
    state = {}
    _run(trello, state, board_ids=())
    trello.get = lambda path, params=None: {"error": "nope"} if path.endswith("/boards") else {}
    with pytest.raises(TrelloAPIError, match="board list"):
        _run(trello, state, board_ids=())
    assert BOARD in state["boards"]


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
    stats = _stats()
    rows = _run(trello, state, stats=stats)
    assert _ids(rows) == [f"board:{BOARD}", "card:c1"]
    assert stats["deleted"] == 2 and stats["emitted"] == 0


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
    rows = _run(trello, {}, board_ids=(), workspace_id="acme")
    assert {r["id"] for r in rows} == {"board:b2"}
    rows = _run(trello, {}, board_ids=(), include_archived=True)
    assert {r["id"] for r in rows} == {f"board:{BOARD}", "board:b2", "board:b3"}


def test_board_comments_page_backwards_past_1000(trello):
    trello.add_card("c1", "Export")
    for index in range(1005):
        trello.comment("c1", f"comment {index}")
    walker = _Walker(trello, _config(), {}, _stats(), None)
    assert len(list(walker._board_comments(BOARD))) == 1005


def test_board_comments_raise_when_the_cursor_does_not_advance():
    class Stuck:
        def get(self, path, params=None):
            return [{"id": "0" * 24}] * 1000

    walker = _Walker(Stuck(), _config(), {}, _stats(), None)
    with pytest.raises(TrelloAPIError, match="did not advance"):
        list(walker._board_comments(BOARD))


# ---------------------------------------------------------------------------
# trello_source factory
# ---------------------------------------------------------------------------
def test_source_declares_document_path_and_scope(trello):
    source = trello_source([BOARD], service=trello)
    assert getattr(source, dlt_utils.DOCUMENT_SOURCE_ATTR) == "trello"
    assert getattr(source, dlt_utils.PIPELINE_SCOPE_ATTR) == "trello_b1"
    assert source.name == "trello_b1"
    spaced = trello_source(workspace_id="Acme Team", client=trello).name
    dashed = trello_source(workspace_id="acme-team", client=trello).name
    assert spaced.startswith("trello_acme_team_") and dashed.startswith("trello_acme_team_")
    assert spaced != dashed
    assert trello_source(workspace_id="acme", service=trello).name == "trello_acme"


@pytest.mark.parametrize("board_ids", [["b1", "b2"], None, []])
def test_several_or_all_boards_need_a_resource_name(trello, board_ids):
    # Two accounts syncing all their boards into one dataset would share a constant name.
    with pytest.raises(ValueError, match="resource_name"):
        trello_source(board_ids, service=trello)
    assert trello_source(board_ids, service=trello, resource_name="trello_team").name == (
        "trello_team"
    )


def test_board_ids_as_a_bare_string_raises(trello):
    with pytest.raises(TypeError, match="not a string"):
        trello_source("abc", service=trello)


def test_a_cognee_without_node_sets_is_refused(trello, monkeypatch):
    monkeypatch.setattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 1)
    with pytest.raises(RuntimeError, match="node sets"):
        trello_source([BOARD], service=trello)


def test_missing_credentials_raise_without_reading_the_environment(monkeypatch):
    monkeypatch.setenv("TRELLO_TOKEN", "from-env")
    with pytest.raises(ValueError, match="api_key and token"):
        trello_source([BOARD], api_key="key")


def test_client_is_built_with_keyword_arguments(monkeypatch):
    from cognee_community_connector_trello import trello as trello_module

    seen = {}
    monkeypatch.setattr(trello_module, "TrelloClient", lambda **kwargs: seen.update(kwargs) or "c")
    trello_source([BOARD], api_key="key", token="tok")
    assert seen == {"api_key": "key", "token": "tok", "check_active": None}


@pytest.mark.parametrize("name", ["", "Trello-Team", "trello team", "t" * 65, 7])
def test_invalid_resource_name_is_rejected(trello, name):
    with pytest.raises(ValueError, match="resource_name"):
        trello_source([BOARD], service=trello, resource_name=name)


@pytest.mark.parametrize("workspace", ["日本チーム", "---", "Ünïcödé"])
def test_derived_resource_name_is_valid_without_ascii_letters(trello, workspace):
    name = trello_source(workspace_id=workspace, service=trello).name
    assert name.startswith("trello_") and "__" not in name


def test_derived_resource_name_stays_valid_for_a_long_workspace(trello):
    names = {
        trello_source(workspace_id=workspace, service=trello).name
        for workspace in ("a" * 80, "a" * 80 + "b")
    }
    assert len(names) == 2 and all(len(name) <= 64 for name in names)


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


def _staged(pipeline, table="trello_b1"):
    with (
        pipeline.sql_client() as client,
        client.execute_query(f"SELECT id FROM {table}") as cursor,
    ):
        return {row[0] for row in cursor.fetchall()}


def test_pipeline_resyncs_only_changes_and_forgets_deletions(dlt_mod, tmp_path, trello):
    trello.add_card("c1", "Export")
    trello.add_card("c2", "Dark mode")
    pipeline = _pipeline(dlt_mod, tmp_path)

    pipeline.run(trello_source([BOARD], service=trello))
    assert _staged(pipeline) == {f"board:{BOARD}", "card:c1", "card:c2"}

    # The cursor lives in dlt state, so a fresh source object resumes from it.
    source = trello_source([BOARD], service=trello)
    pipeline.run(source)
    assert source.cognee_sync_stats["emitted"] == 0

    trello.boards[BOARD]["cards"] = [trello.card("c1")]
    trello.act(BOARD, "deleteCard", card={"id": "c2"})
    pipeline.run(trello_source([BOARD], service=trello))
    assert _staged(pipeline) == {f"board:{BOARD}", "card:c1"}


def test_owned_client_is_closed_and_stats_carry_every_key(dlt_mod, tmp_path, trello, monkeypatch):
    from cognee_community_connector_trello import trello as trello_module

    closed = []

    class Owned:
        def __init__(self, **kwargs):
            pass

        def get(self, path, params=None):
            return trello.get(path, params)

        def close(self):
            closed.append(True)

    monkeypatch.setattr(trello_module, "TrelloClient", Owned)
    source = trello_source([BOARD], api_key="key", token="tok")
    _pipeline(dlt_mod, tmp_path).run(source)
    assert closed == [True]
    assert source.cognee_sync_stats["failed"] == 0
    assert {"failed_rate_limit", "failed_auth", "failed_budget"} <= set(source.cognee_sync_stats)


def test_two_sources_in_one_dataset_keep_their_own_cards(dlt_mod, tmp_path, trello):
    trello.add_board("b2", "Ops")
    trello.add_card("c1", "Export")
    trello.add_card("c2", "Deploy", board_id="b2")
    pipeline = _pipeline(dlt_mod, tmp_path)

    for _ in range(2):
        pipeline.run(trello_source(["b1"], service=trello))
        pipeline.run(trello_source(["b2"], service=trello))
    assert _staged(pipeline, "trello_b1") == {"board:b1", "card:c1"}
    assert _staged(pipeline, "trello_b2") == {"board:b2", "card:c2"}


def test_failed_run_keeps_staging_and_cursor(dlt_mod, tmp_path, trello):
    trello.add_card("c1", "Export")
    pipeline = _pipeline(dlt_mod, tmp_path)
    pipeline.run(trello_source([BOARD], service=trello))

    trello.boards[BOARD]["cards"] = []
    trello.act(BOARD, "deleteCard", card={"id": "c1"})
    trello.errors[BOARD] = 500
    with pytest.raises(PipelineStepFailed):
        pipeline.run(trello_source([BOARD], service=trello))
    assert _staged(pipeline) == {f"board:{BOARD}", "card:c1"}

    trello.errors.clear()
    pipeline.run(trello_source([BOARD], service=trello))
    assert _staged(pipeline) == {f"board:{BOARD}"}
