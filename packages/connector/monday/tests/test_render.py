"""Tests for rendering a monday.com item as a document.

The fixtures follow the shapes the monday.com API (2026-07) returned for a real
board, with neutral names.
"""

import json
import random

from cognee_community_connector_monday.monday import _render_item

BOARD = {
    "id": "100",
    "name": "Launch plan",
    "columns": [
        {"id": "name", "title": "Name", "type": "name"},
        {"id": "subtasks", "title": "Subitems", "type": "subtasks"},
        {"id": "owner", "title": "Owner", "type": "people"},
        {"id": "status", "title": "Status", "type": "status"},
        {"id": "due", "title": "Due date", "type": "date"},
        {"id": "risk", "title": "Risk", "type": "status"},
        {"id": "notes", "title": "Notes", "type": "text"},
        {"id": "score", "title": "Score", "type": "formula"},
        {"id": "touched", "title": "Last updated", "type": "last_updated"},
        {"id": "timer", "title": "Time tracking", "type": "time_tracking"},
        {"id": "files", "title": "Files", "type": "file"},
    ],
}


def column(column_id, column_type, text, **extra):
    # ``value`` mimics the raw JSON monday.com returns, with its volatile changed_at.
    return {
        "id": column_id,
        "type": column_type,
        "text": text,
        "value": json.dumps({"changed_at": str(random.random())}),
        **extra,
    }


def update(update_id, author, created_at, text, replies=()):
    return {
        "id": update_id,
        "text_body": text,
        "created_at": created_at,
        "creator": {"name": author},
        "replies": list(replies),
    }


def make_item(**overrides):
    item = {
        "id": "200",
        "name": "Ship the beta",
        "url": "https://acme.monday.com/boards/100/pulses/200",
        "group": {"title": "This week"},
        "parent_item": None,
        "description": None,
        "column_values": [
            column("subtasks", "subtasks", None),
            column("owner", "people", "Alex Doe, Sam Roe"),
            column("status", "status", "Working on it"),
            column("due", "date", "2026-10-20"),
            column("risk", "status", "High"),
            column("notes", "text", "Needs  the\nnew pricing page"),
            column("score", "formula", "", display_value="13"),
            column("touched", "last_updated", "2026-10-15 09:12"),
            column("timer", "time_tracking", "00:42:10"),
            column("files", "file", "https://files.monday.com/signed?token=abc"),
        ],
    }
    item.update(overrides)
    return item


UPDATES = [
    update(
        "2",
        "Sam Roe",
        "2026-10-15T10:00:00Z",
        "Blocked on the API keys",
        replies=[update("4", "Alex Doe", "2026-10-15T12:00:00Z", "Asked IT today")],
    ),
    update("1", "Alex Doe", "2026-10-14T09:00:00Z", "Kickoff done"),
]


def test_item_renders_like_a_linear_issue():
    row = _render_item(make_item(), BOARD, UPDATES)

    assert row == {
        "id": "monday:200",
        "title": "Ship the beta",
        "content": "\n".join(
            [
                "Board: Launch plan",
                "Group: This week",
                "State: Working on it",
                "Assignee: Alex Doe, Sam Roe",
                # Other columns follow in column id order: due, notes, risk, score.
                "Due date: 2026-10-20",
                "Notes: Needs the new pricing page",
                "Risk: High",
                "Score: 13",
                "",
                "Updates:",
                "Alex Doe (2026-10-14): Kickoff done",
                "Sam Roe (2026-10-15): Blocked on the API keys",
                "  Reply from Alex Doe (2026-10-15): Asked IT today",
            ]
        ),
        "url": "https://acme.monday.com/boards/100/pulses/200",
        "_deleted": False,
    }


def test_volatile_columns_are_left_out():
    content = _render_item(make_item(), BOARD, [])["content"]

    assert "Last updated" not in content
    assert "00:42:10" not in content
    assert "signed?token" not in content


def test_rendering_is_deterministic():
    # Same item, different response order and different raw values: same text.
    first = _render_item(make_item(), BOARD, UPDATES)
    shuffled = make_item()
    random.shuffle(shuffled["column_values"])
    second = _render_item(shuffled, BOARD, list(reversed(UPDATES)))

    assert first == second


def test_reordering_people_is_not_a_change():
    reordered = make_item()
    reordered["column_values"][1] = column("owner", "people", "Sam Roe, Alex Doe")

    assert _render_item(reordered, BOARD, []) == _render_item(make_item(), BOARD, [])


def test_moving_a_column_on_the_board_is_not_a_change():
    # Dragging a column must not change every item's text and re-extract the board.
    moved = {**BOARD, "columns": list(reversed(BOARD["columns"]))}

    assert _render_item(make_item(), moved, []) == _render_item(make_item(), BOARD, [])


def test_board_without_status_or_people_columns():
    board = {"name": "Reading list", "columns": [{"id": "notes", "title": "Notes", "type": "text"}]}
    item = make_item(column_values=[column("notes", "text", "Chapter 3")])

    content = _render_item(item, board, [])["content"]

    assert "State:" not in content
    assert "Assignee:" not in content
    assert "Notes: Chapter 3" in content


def test_subitem_names_its_parent():
    item = make_item(parent_item={"name": "Ship the beta"}, name="Write release notes")

    content = _render_item(item, BOARD, [])["content"]

    assert "Parent: Ship the beta" in content


def test_empty_updates_and_values_are_skipped():
    updates = [update("9", "Alex Doe", "2026-10-15T10:00:00Z", "   ")]
    item = make_item(column_values=[column("status", "status", "")])

    content = _render_item(item, BOARD, updates)["content"]

    assert "State" not in content
    assert "Updates" not in content


def test_description_blocks_render_as_markdown_in_order():
    # Block shapes as monday.com returned them for a real item description.
    blocks = [
        {"id": "c", "type": "bulleted list", "position": 262144, "content": delta("write tests")},
        {"id": "a", "type": "large title", "position": 131072, "content": delta("Goal\n")},
        {"id": "b", "type": "normal text", "position": 196608, "content": delta("Ship ", "it.")},
        {
            "id": "d",
            "type": "bulleted list",
            "position": 327680,
            "content": delta("cover deletes", indentation=1),
        },
    ]
    item = make_item(column_values=[], description={"id": "9", "blocks": blocks})

    content = _render_item(item, BOARD, [])["content"]

    assert content.endswith("# Goal\nShip it.\n- write tests\n  - cover deletes")


def test_item_with_no_content_falls_back_to_its_name():
    board = {"name": "", "columns": []}
    item = make_item(name="  ", group=None, column_values=[])

    row = _render_item(item, board, [])

    assert row["title"] == "Untitled item"
    assert row["content"] == "Untitled item"


def delta(*inserts, **extra):
    return json.dumps({"deltaFormat": [{"insert": text} for text in inserts], **extra})
