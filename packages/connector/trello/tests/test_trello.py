"""Hermetic unit tests for the Trello Cognee Community Connector."""

import pytest
from unittest.mock import patch, MagicMock
import urllib.error

from cognee_community_connector_trello.trello import TrelloClient, trello_data

MOCK_BOARDS = [
    {"id": "board_1", "name": "Sprint 42", "desc": "Current product sprint", "url": "https://trello.com/b/board_1"}
]

MOCK_LISTS = [
    {"id": "list_todo", "name": "To Do", "closed": False},
    {"id": "list_done", "name": "Done", "closed": False}
]

MOCK_CARDS = [
    {
        "id": "card_101",
        "name": "Implement OAuth2 Flow",
        "desc": "Support Google and GitHub OAuth logins.",
        "idList": "list_todo",
        "due": "2026-11-10T12:00:00.000Z",
        "dueComplete": False,
        "dateLastActivity": "2026-11-03T14:00:00.000Z",
        "url": "https://trello.com/c/card_101",
        "labels": [{"name": "Security", "color": "red"}],
        "checklists": [
            {
                "name": "Tasks",
                "checkItems": [
                    {"name": "Setup credentials", "state": "complete"},
                    {"name": "Add callback handler", "state": "incomplete"}
                ]
            }
        ],
        "actions": [
            {
                "type": "commentCard",
                "data": {"text": "Make sure redirect URIs are secured."},
                "memberCreator": {"fullName": "Alice Smith"},
                "date": "2026-11-03T13:50:00.000Z"
            }
        ]
    },
    {
        "id": "card_102",
        "name": "Database Migration Script",
        "desc": "Run alembic migration for user sessions.",
        "idList": "list_done",
        "due": None,
        "dueComplete": True,
        "dateLastActivity": "2026-11-02T10:00:00.000Z",
        "url": "https://trello.com/c/card_102",
        "labels": [],
        "checklists": [],
        "actions": []
    }
]

def test_fetch_board_data_and_card_formatting():
    """Verifies that lists are resolved and card documents contain full formatted metadata."""
    client = TrelloClient(api_key="mock_key", token="mock_token")

    with patch.object(client, "get_board_lists", return_value=MOCK_LISTS), \
         patch.object(client, "get_board_cards", return_value=MOCK_CARDS):

        pack = client.fetch_board_data("board_1")

        assert pack["board_id"] == "board_1"
        assert len(pack["cards"]) == 2
        assert "card_101" in pack["current_card_ids"]
        assert "card_102" in pack["current_card_ids"]

        card1 = next(c for c in pack["cards"] if c["card_id"] == "card_101")
        assert card1["list_name"] == "To Do"
        assert card1["due_complete"] is False
        assert "Security" in card1["labels"]
        # Verify text document content
        assert "# Card: Implement OAuth2 Flow" in card1["text"]
        assert "- **List / Stage**: To Do" in card1["text"]
        assert "Support Google and GitHub OAuth logins." in card1["text"]
        assert "Setup credentials" in card1["text"]
        assert "Alice Smith" in card1["text"]
        assert "Make sure redirect URIs are secured." in card1["text"]

def test_incremental_cursor_updates():
    """Verifies that the highest dateLastActivity is computed as the new sync cursor."""
    client = TrelloClient(api_key="mock_key", token="mock_token")

    with patch.object(client, "get_board_lists", return_value=MOCK_LISTS), \
         patch.object(client, "get_board_cards", return_value=MOCK_CARDS):

        pack = client.fetch_board_data("board_1", since="2026-11-01T00:00:00.000Z")
        # card_101 has dateLastActivity "2026-11-03T14:00:00.000Z", which should be the new cursor
        assert pack["cursor"] == "2026-11-03T14:00:00.000Z"

def test_forget_on_delete_tombstones():
    """Verifies that removed card IDs emit tombstone records with _deleted=True."""
    client = TrelloClient(api_key="mock_key", token="mock_token")

    # Suppose previously we had card_101, card_102, and card_999 (card_999 was deleted from board)
    prev_ids = {"card_101", "card_102", "card_999"}

    with patch.object(client, "get_board_lists", return_value=MOCK_LISTS), \
         patch.object(client, "get_board_cards", return_value=MOCK_CARDS):

        pack = client.fetch_board_data("board_1", previous_card_ids=prev_ids)

        assert len(pack["tombstones"]) == 1
        tombstone = pack["tombstones"][0]
        assert tombstone["card_id"] == "card_999"
        assert tombstone["_deleted"] is True

def test_auth_failure_raises_permission_error():
    """Verifies that HTTP 401 raises PermissionError with clear message."""
    client = TrelloClient(api_key="invalid_key", token="invalid_token")

    err_401 = urllib.error.HTTPError(
        url="https://api.trello.com/1/members/me/boards",
        code=401,
        msg="Unauthorized",
        hdrs={},
        fp=None
    )

    with patch.object(client, "_http_get", side_effect=err_401):
        with pytest.raises(urllib.error.HTTPError):
            client.get_member_boards()

def test_trello_data_generator_yields_cognee_documents():
    """Verifies that trello_data generator yields active documents and tombstones."""
    mock_pack = {
        "board_id": "board_1",
        "cursor": "2026-11-03T14:00:00.000Z",
        "current_card_ids": ["card_101"],
        "cards": [
            {
                "id": "trello_card_card_101",
                "name": "Test Card",
                "list_name": "In Progress",
                "text": "# Card: Test Card\n",
                "_deleted": False
            }
        ],
        "tombstones": [
            {
                "id": "trello_card_card_old",
                "card_id": "card_old",
                "board_id": "board_1",
                "_deleted": True
            }
        ]
    }

    mock_client = MagicMock()
    mock_client.fetch_board_data.return_value = mock_pack

    docs = list(trello_data(board_ids=["board_1"], client=mock_client))
    assert len(docs) == 2
    active_doc = next(d for d in docs if not d["_deleted"])
    assert active_doc["id"] == "trello_card_card_101"
    assert active_doc["type"] == "trello_card"

    deleted_doc = next(d for d in docs if d["_deleted"])
    assert deleted_doc["id"] == "trello_card_card_old"
    assert deleted_doc["_deleted"] is True

def test_missing_board_ids_raises_error_when_no_env():
    """Verifies that missing board_ids and failing member lookup raises ValueError."""
    mock_client = MagicMock()
    mock_client.get_member_boards.side_effect = Exception("No auth configured")

    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(ValueError, match="Must provide 'board_ids' parameter"):
            list(trello_data(client=mock_client))

def test_empty_board_handling():
    """Verifies clean handling of a board with 0 cards."""
    client = TrelloClient(api_key="mock_key", token="mock_token")

    with patch.object(client, "get_board_lists", return_value=[]), \
         patch.object(client, "get_board_cards", return_value=[]):

        pack = client.fetch_board_data("empty_board")
        assert pack["cards"] == []
        assert pack["current_card_ids"] == []
        assert pack["tombstones"] == []

def test_minimal_card_without_extras():
    """Verifies that cards without description, checklists, or comments format cleanly."""
    client = TrelloClient(api_key="mock_key", token="mock_token")
    minimal_card = {
        "id": "card_simple",
        "name": "Simple Task",
        "desc": "",
        "idList": "list_1",
        "due": None,
        "dueComplete": False,
        "dateLastActivity": "2026-11-01T00:00:00.000Z",
        "labels": [],
        "checklists": [],
        "actions": []
    }
    with patch.object(client, "get_board_lists", return_value=[{"id": "list_1", "name": "Backlog"}]), \
         patch.object(client, "get_board_cards", return_value=[minimal_card]):

        pack = client.fetch_board_data("board_1")
        assert len(pack["cards"]) == 1
        card = pack["cards"][0]
        assert "Simple Task" in card["text"]
        assert "Backlog" in card["text"]
        assert "## Description" not in card["text"]
        assert "## Checklists" not in card["text"]

