"""Tests for change detection: which items a sync re-reads, and which it skips."""

import pytest
from fake_monday import FakeMonday

from cognee_community_connector_monday.monday import MondayAPIError, _iter_rows

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
    with pytest.raises(MondayAPIError, match="not found or are not shared"):
        list(_iter_rows(monday, ["999"], {}, {}))


def test_rows_are_documents_with_a_stable_id(monday):
    rows, _ = sync(monday, {})

    row = next(r for r in rows if r["title"] == "Write docs")
    assert row["id"] == "monday:2"
    assert row["_deleted"] is False
    assert row["url"].endswith("/pulses/2")
