"""Rendering recordings into cognee document rows (pure functions)."""

from cognee.tasks.ingestion.dlt_utils import NODE_SET_COLUMN

from cognee_community_connector_basecamp.basecamp import html_to_text, recording_to_row


def _rec(**overrides):
    rec = {
        "id": 1,
        "type": "Message",
        "status": "active",
        "title": "Q4 launch plan\n",
        "content": '<p dir="auto">The <strong>payments</strong> team owns checkout.</p>',
        "created_at": "2026-10-08T18:01:14.137Z",
        "updated_at": "2026-10-08T18:01:14.137Z",
        "app_url": "https://3.basecamp.com/1/buckets/2/messages/1",
        "bucket": {"name": "My Project"},
        "creator": {"name": "Anand"},
    }
    rec.update(overrides)
    return rec


def test_html_to_text_keeps_words_and_breaks_blocks():
    html = (
        '<div><p dir="auto">Hello <strong>bold</strong> &amp; more</p>'
        "<ul><li>a</li><li>b</li></ul></div>"
    )
    assert html_to_text(html) == "Hello bold & more\n\n- a\n\n- b"
    assert html_to_text(None) == ""


def test_message_row_shape_and_trailing_newline_stripped():
    row = recording_to_row(_rec())
    assert row["id"] == "message:1"
    assert row["title"] == "Q4 launch plan"
    assert row["url"].endswith("/messages/1")
    assert row["_deleted"] is False
    assert "Project: My Project" in row["content"]
    assert "Author: Anand" in row["content"]
    assert "The payments team owns checkout." in row["content"]
    assert row[NODE_SET_COLUMN] == ["My Project"]


def test_completed_todo_carries_list_state_and_due_date():
    row = recording_to_row(
        _rec(
            type="Todo",
            title="Write release notes",
            content="Write release notes",
            description='<p dir="auto">Include the Q4 changes.</p>',
            completed=True,
            due_on="2026-10-09",
            parent={"id": 9, "type": "Todolist", "title": "Launch tasks"},
        )
    )
    assert row["id"] == "todo:1"
    assert "To-do list: Launch tasks" in row["content"]
    assert "Completed: yes" in row["content"]
    assert "Due: 2026-10-09" in row["content"]
    assert "Include the Q4 changes." in row["content"]


def test_comment_mentions_its_parent():
    row = recording_to_row(
        _rec(
            type="Comment",
            title="",
            content='<p dir="auto">Repro only on Safari.</p>',
            parent={"id": 5, "type": "Todo", "title": "Fix login bug"},
        )
    )
    assert row["id"] == "comment:1"
    assert row["title"] == 'Comment on to-do "Fix login bug"'
    assert 'On to-do: "Fix login bug"' in row["content"]
    assert "Repro only on Safari." in row["content"]


def test_archived_items_say_so():
    assert "Status: archived" in recording_to_row(_rec(status="archived"))["content"]


def test_rendering_is_deterministic_and_ignores_updated_at():
    a = recording_to_row(_rec())
    b = recording_to_row(_rec(updated_at="2030-01-01T00:00:00Z"))
    assert a == b


def test_unsupported_type_or_empty_item_is_skipped():
    assert recording_to_row(_rec(type="Kanban::Card")) is None
    assert recording_to_row(_rec(title="", content="")) is None
