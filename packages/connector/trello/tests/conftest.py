"""In-memory stand-in for the Trello REST API, shared by the test modules."""

import itertools
import re

import pytest

from cognee_community_connector_trello.trello import TrelloAPIError

BOARD = "b1"
PRIYA = {"id": "m1", "fullName": "Priya Shah"}
SAM = {"id": "m2", "fullName": "Sam Lee"}


class FakeTrello:
    """Implements ``get`` like Trello: actions newest first, paged with ``before``.

    ``since`` is treated as inclusive here, so the source must not count the
    cursor action itself as a change.
    """

    def __init__(self):
        self.boards: dict[str, dict] = {}
        self.actions: dict[str, list[dict]] = {}
        self.bad_token = False
        self.errors: dict[str, int] = {}
        self.calls: list[tuple[str, dict]] = []
        self._ids = itertools.count(1)
        self.add_board(BOARD, "Product")

    def _id(self) -> str:
        return f"{next(self._ids):024x}"

    def add_board(self, board_id, name, *, closed=False, organization=None):
        self.boards[board_id] = {
            "id": board_id,
            "name": name,
            "desc": "Roadmap and bugs",
            "closed": closed,
            "url": f"https://trello.com/b/{board_id}",
            "dateLastActivity": "2026-10-01T00:00:00.000Z",
            "idOrganization": organization,
            "lists": [
                {"id": "l1", "name": "To do", "pos": 1, "closed": False},
                {"id": "l2", "name": "Done", "pos": 2, "closed": False},
            ],
            "labels": [{"id": "lb1", "name": "bug", "color": "red"}],
            "members": [PRIYA, SAM],
            "cards": [],
            "checklists": [],
        }
        self.actions[board_id] = []

    def act(self, board_id, kind="updateCard", **data):
        """Record an action in the board feed and bump its activity, like Trello does."""
        action = {
            "id": self._id(),
            "type": kind,
            "date": "2026-10-05T10:00:00.000Z",
            "data": data,
            "memberCreator": PRIYA,
        }
        self.actions[board_id].append(action)
        self.boards[board_id]["dateLastActivity"] = f"2026-10-05T10:00:{len(self.actions):02d}Z"
        return action

    def add_card(self, card_id, name, board_id=BOARD, **fields):
        card = {
            "id": card_id,
            "name": name,
            "desc": "",
            "idList": "l1",
            "labels": [],
            "idMembers": [],
            "closed": False,
            "shortUrl": f"https://trello.com/c/{card_id}",
            **fields,
        }
        self.boards[board_id]["cards"].append(card)
        self.act(board_id, "createCard", card={"id": card_id})
        return card

    def comment(self, card_id, text, board_id=BOARD, author=SAM):
        action = self.act(board_id, "commentCard", card={"id": card_id}, text=text)
        action["memberCreator"] = author
        return action

    def card(self, card_id, board_id=BOARD):
        return next(c for c in self.boards[board_id]["cards"] if c["id"] == card_id)

    def get(self, path, params=None):
        params = dict(params or {})
        self.calls.append((path, params))
        if path == "/members/me":
            if self.bad_token:
                raise TrelloAPIError("Trello request failed: HTTP 401", status=401)
            return {"id": "me"}
        if path == "/members/me/boards" or path.startswith("/organizations/"):
            organization = path.split("/")[2] if path.startswith("/organizations/") else None
            return [
                {"id": b["id"]}
                for b in self.boards.values()
                if (organization is None or b["idOrganization"] == organization)
                and (params["filter"] == "all" or not b["closed"])
            ]
        match = re.fullmatch(r"/boards/(\w+)(/actions)?", path)
        if not match:
            raise AssertionError(f"unexpected path {path}")
        board_id = match.group(1)
        if board_id in self.errors:
            status = self.errors[board_id]
            raise TrelloAPIError(f"Trello request failed: HTTP {status}", status=status)
        if board_id not in self.boards:
            raise TrelloAPIError("Trello request failed: HTTP 404", status=404)
        if not match.group(2):
            return self.boards[board_id]
        actions = sorted(self.actions[board_id], key=lambda a: a["id"], reverse=True)
        if params.get("filter"):
            actions = [a for a in actions if a["type"] in params["filter"].split(",")]
        if params.get("since"):
            actions = [a for a in actions if a["id"] >= params["since"]]
        if params.get("before"):
            actions = [a for a in actions if a["id"] < params["before"]]
        return actions[: params["limit"]]


@pytest.fixture
def trello():
    return FakeTrello()
