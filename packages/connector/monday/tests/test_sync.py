"""Tests for change detection: which items a sync re-reads, and which it skips."""

import pytest
from fake_monday import FakeMonday

from cognee_community_connector_monday.monday import MondayAPIError, MondayAuthError, _iter_rows

BOARD, SUBITEMS = "100", "101"


@pytest.fixture
def monday():
    fake = FakeMonday()
    fake.add_board(BOARD, "Launch plan", subitems_board=SUBITEMS)
    fake.add_board(SUBITEMS, "Subitems of Launch plan")
    fake.add_item("1", BOARD, "Ship the beta", status="Working on it")
    fake.add_item("2", BOARD, "Write docs", status="Done")
    fake.add_item("3", SUBITEMS, "Record demo", parent="1")
    return fake


def sync(monday, state, **kwargs):
    stats = {}
    monday.calls.clear()
    rows = list(_iter_rows(monday, [BOARD], state, stats, **kwargs))
    return rows, stats


def titles(rows):
    return sorted(row["title"] for row in rows)


def test_first_sync_reads_every_item_and_the_subitem_board(monday):
    rows, stats = sync(monday, {})

    assert titles(rows) == ["Record demo", "Ship the beta", "Write docs"]
    assert (
        "Parent: Ship the beta" in next(r for r in rows if r["title"] == "Record demo")["content"]
    )
    assert stats == {"scanned": 3, "changed": 3}


def test_unchanged_boards_fetch_nothing(monday):
    state = {}
    sync(monday, state)

    rows, stats = sync(monday, state)

    assert rows == []
    assert stats == {"scanned": 3}
    assert "ItemDetails" not in monday.calls


def test_a_column_change_rereads_only_that_item(monday):
    state = {}
    sync(monday, state)
    monday.set_status("2", "Stuck")

    rows, _ = sync(monday, state)

    assert titles(rows) == ["Write docs"]
    assert "State: Stuck" in rows[0]["content"]


def test_a_new_comment_is_caught_although_updated_at_did_not_move(monday):
    state = {}
    sync(monday, state)
    monday.post_update("1", "Blocked on the API keys")

    rows, _ = sync(monday, state)

    assert titles(rows) == ["Ship the beta"]
    assert "Alex Doe (2026-10-17): Blocked on the API keys" in rows[0]["content"]


def test_an_edited_comment_is_caught(monday):
    update = monday.post_update("1", "Blocked on the API keys")
    state = {}
    sync(monday, state)
    monday.edit_update("1", update["id"], "Unblocked, keys arrived")

    rows, _ = sync(monday, state)

    assert "Unblocked, keys arrived" in rows[0]["content"]
    assert "Blocked on the API keys" not in rows[0]["content"]


def test_a_new_reply_is_caught(monday):
    update = monday.post_update("1", "Blocked on the API keys")
    state = {}
    sync(monday, state)
    monday.reply("1", update["id"], "Asked IT today")

    rows, _ = sync(monday, state)

    assert "Reply from Sam Roe (2026-10-17): Asked IT today" in rows[0]["content"]


def test_a_deleted_comment_is_caught(monday):
    update = monday.post_update("1", "Temporary note")
    state = {}
    sync(monday, state)
    monday.items["1"]["updates"].remove(update)

    rows, _ = sync(monday, state)

    assert titles(rows) == ["Ship the beta"]
    assert "Temporary note" not in rows[0]["content"]


def test_a_new_item_is_read(monday):
    state = {}
    sync(monday, state)
    monday.add_item("4", BOARD, "Plan the launch party")

    rows, _ = sync(monday, state)

    assert titles(rows) == ["Plan the launch party"]


def test_full_refresh_rereads_everything(monday):
    state = {}
    sync(monday, state)

    rows, _ = sync(monday, state, full_refresh=True)

    assert len(rows) == 3


def test_changed_items_are_fetched_a_hundred_per_call(monday):
    for n in range(250):
        monday.add_item(f"x{n}", BOARD, f"Task {n}")

    rows, _ = sync(monday, {})

    assert len(rows) == 253
    # 2 boards, 250 + 2 items on the main board and 1 on the sub-item board.
    assert monday.calls.count("ItemDetails") == 4


def test_an_item_archived_after_the_listing_is_not_emitted(monday):
    original = monday.paginate

    def archive_during_listing(*args, **kwargs):
        yield from original(*args, **kwargs)
        monday.archive("2")

    monday.paginate = archive_during_listing

    rows, _ = sync(monday, {})

    assert "Write docs" not in titles(rows)


def test_a_board_the_token_cannot_see_raises_instead_of_looking_empty(monday):
    with pytest.raises(MondayAPIError, match="No monday\\.com board could be synced"):
        list(_iter_rows(monday, ["999"], {}, {}))


def test_rows_are_documents_with_a_stable_id(monday):
    rows, _ = sync(monday, {})

    row = next(r for r in rows if r["title"] == "Write docs")
    assert row["id"] == "monday:2"
    assert row["_deleted"] is False
    assert row["url"].endswith("/pulses/2")


# -- forget-on-delete --------------------------------------------------------


def forgotten(rows):
    return sorted(row["id"] for row in rows if row.get("_deleted"))


@pytest.mark.parametrize("remove", ["archive", "delete", "purge"])
def test_an_archived_deleted_or_purged_item_is_forgotten(monday, remove):
    state = {}
    sync(monday, state)
    getattr(monday, remove)("2")

    rows, stats = sync(monday, state)

    assert forgotten(rows) == ["monday:2"]
    assert stats["deleted"] == 1
    assert "2" not in state["boards"][BOARD]["items"]


def test_a_forgotten_item_is_forgotten_once(monday):
    state = {}
    sync(monday, state)
    monday.delete("2")
    sync(monday, state)

    rows, _ = sync(monday, state)

    assert rows == []


def test_an_item_still_active_on_the_board_is_not_forgotten(monday):
    # The listing missed an item that the lookup says is still there:
    # one contradicted signal must never delete.
    state = {}
    sync(monday, state)
    original = monday.paginate

    def listing_that_drops_item_2(*args, **kwargs):
        yield from (item for item in original(*args, **kwargs) if item["id"] != "2")

    monday.paginate = listing_that_drops_item_2

    rows, _ = sync(monday, state)

    assert forgotten(rows) == []
    assert "2" in state["boards"][BOARD]["items"]


def test_an_item_moved_to_an_unsynced_board_is_forgotten(monday):
    monday.add_board("200", "Someone else's board")
    state = {}
    sync(monday, state)
    monday.move("2", "200")

    rows, _ = sync(monday, state)

    assert forgotten(rows) == ["monday:2"]


def test_an_item_moved_between_synced_boards_is_kept(monday):
    monday.add_board("200", "Second board")
    state = {}
    list(_iter_rows(monday, [BOARD, "200"], state, {}))
    monday.move("2", "200")

    rows = list(_iter_rows(monday, [BOARD, "200"], state, {}))

    assert forgotten(rows) == []
    assert [row["title"] for row in rows] == ["Write docs"]
    assert "2" in state["boards"]["200"]["items"]
    assert "2" not in state["boards"][BOARD]["items"]


@pytest.mark.parametrize("board_state", ["archived", "deleted"])
def test_an_archived_or_deleted_board_forgets_its_items(monday, board_state):
    state = {}
    sync(monday, state)
    monday.set_board_state(BOARD, board_state)

    rows, _ = sync(monday, state)

    # Its sub-item board is no longer reached either, so sub-items go too.
    assert forgotten(rows) == ["monday:1", "monday:2", "monday:3"]
    assert state["boards"] == {}


def test_a_board_removed_from_the_selection_forgets_its_items(monday):
    monday.add_board("200", "Second board")
    monday.add_item("9", "200", "Only on the second board")
    state = {}
    list(_iter_rows(monday, [BOARD, "200"], state, {}))

    rows = list(_iter_rows(monday, [BOARD], state, {}))

    assert forgotten(rows) == ["monday:9"]


def test_losing_access_to_a_board_raises_and_forgets_nothing(monday):
    state = {}
    sync(monday, state)
    monday.revoke_access(BOARD)

    with pytest.raises(MondayAPIError, match="Nothing was forgotten"):
        sync(monday, state)
    assert set(state["boards"][BOARD]["items"]) == {"1", "2"}
    # The sub-item board is not forgotten either: its parent board failed.
    assert set(state["boards"][SUBITEMS]["items"]) == {"3"}


def test_an_error_during_the_lookup_forgets_nothing(monday):
    state = {}
    sync(monday, state)
    monday.delete("2")
    original = monday.query

    def failing_lookup(query, variables=None):
        if "ItemStates" in query:
            raise MondayAPIError("boom")
        return original(query, variables)

    monday.query = failing_lookup

    rows, stats = sync(monday, state)

    assert forgotten(rows) == []
    assert "2" in state["boards"][BOARD]["items"]
    assert stats["failed"] == 1


def answer_with(monday, operation, change):
    """Pass every answer to ``operation`` queries through ``change`` first."""
    original = monday.query

    def query(query, variables=None):
        answer = original(query, variables)
        return change(answer) if f"query {operation}" in query else answer

    monday.query = query
    return original


@pytest.mark.parametrize("broken", [{"items": None}, {}])
def test_a_lookup_without_an_items_list_forgets_nothing(monday, broken):
    state = {}
    sync(monday, state)
    monday.delete("2")
    answer_with(monday, "ItemStates", lambda answer: broken)

    rows, stats = sync(monday, state)

    # Read as an empty answer, it would make every item asked about look gone.
    assert forgotten(rows) == []
    assert "2" in state["boards"][BOARD]["items"]
    assert stats["failed"] == 1


def test_an_item_the_fetch_leaves_out_is_asked_for_again(monday):
    state = {}
    sync(monday, state)
    monday.set_status("1", "Stuck")
    original = answer_with(
        monday,
        "ItemDetails",
        lambda answer: {"items": [i for i in answer["items"] if i["id"] != "1"]},
    )

    rows, _ = sync(monday, state)
    assert titles(rows) == []

    monday.query = original
    rows, _ = sync(monday, state)
    assert titles(rows) == ["Ship the beta"]


def test_items_past_the_update_limit_are_counted_and_warned_about(monday, monkeypatch):
    import cognee_community_connector_monday.monday as module

    warnings = []
    monkeypatch.setattr(
        module.logger, "warning", lambda message, *args: warnings.append(message % args)
    )
    for n in range(module._UPDATES_LIMIT):
        monday.post_update("1", f"Update {n}")

    _, stats = sync(monday, {})

    assert stats["updates_cut"] == 1
    assert any("1 item(s) have 100 or more updates" in warning for warning in warnings)


def test_an_empty_board_still_forgets_its_last_item(monday):
    state = {}
    sync(monday, state)
    monday.delete("1")
    monday.delete("2")

    rows, _ = sync(monday, state)

    assert forgotten(rows) == ["monday:1", "monday:2"]


# -- one failing board -------------------------------------------------------


def fail_listing_of(monday, board_id, error):
    original = monday.paginate

    def paginate(query, variables, **kwargs):
        if variables["board"] == [board_id]:
            raise error
        yield from original(query, variables, **kwargs)

    monday.paginate = paginate


def test_a_failing_board_does_not_stop_the_others(monday):
    monday.add_board("200", "Second board")
    monday.add_item("9", "200", "Only on the second board")
    state = {}
    list(_iter_rows(monday, [BOARD, "200"], state, {}))
    saved_before = dict(state["boards"][BOARD]["items"])
    monday.set_status("9", "Done")
    monday.set_status("1", "Done")
    fail_listing_of(monday, BOARD, MondayAPIError("boom"))

    stats = {}
    rows = list(_iter_rows(monday, [BOARD, "200"], state, stats))

    # The healthy board synced; the failing one kept its state for next time.
    assert [row["title"] for row in rows] == ["Only on the second board"]
    assert state["boards"][BOARD]["items"] == saved_before
    assert stats["failed"] == 1


def test_a_network_error_on_one_board_is_isolated_too(monday):
    import requests

    monday.add_board("200", "Second board")
    fail_listing_of(monday, BOARD, requests.ConnectionError("reset"))

    stats = {}
    list(_iter_rows(monday, [BOARD, "200"], {}, stats))

    assert stats["failed"] == 1


def test_a_refused_token_stops_the_whole_sync(monday):
    monday.add_board("200", "Second board")
    fail_listing_of(monday, BOARD, MondayAuthError("refused"))

    with pytest.raises(MondayAuthError):
        list(_iter_rows(monday, [BOARD, "200"], {}, {}))


def test_one_missing_board_does_not_stop_the_others(monday):
    stats = {}
    rows = list(_iter_rows(monday, [BOARD, "999"], {}, stats))

    assert len(rows) == 3
    assert stats["failed"] == 1


def test_the_same_board_selected_twice_is_synced_once(monday):
    rows = list(_iter_rows(monday, [BOARD, BOARD, int(BOARD)], {}, {}))

    assert len(rows) == 3


def test_logs_carry_counts_only(monday, monkeypatch):
    import cognee_community_connector_monday.monday as module

    messages = []

    class Recorder:
        def info(self, message, *args):
            messages.append(message % args)

        warning = info

    monkeypatch.setattr(module, "logger", Recorder())
    monday.post_update("1", "Secret roadmap discussion")
    fail_listing_of(monday, SUBITEMS, MondayAPIError(f"board {SUBITEMS} failed"))

    list(_iter_rows(monday, [BOARD], {}, {}))

    text = " ".join(messages)
    assert messages
    for private in ("Ship the beta", "Secret roadmap", BOARD, SUBITEMS, "monday:"):
        assert private not in text
