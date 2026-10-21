"""In-memory stand-in for the Trello REST API, shared by the test modules."""

import itertools
import re

import pytest

from cognee_community_connector_trello.trello import (
    TrelloAPIError,
    TrelloAuthError,
    TrelloRateLimitedError,
)

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
        self.rate_limited: set[str] = set()
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
        self.boards[board_id]["dateLastActivity"] = f"2026-10-05T10:{len(self.actions):02d}Z"
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

    def move_card(self, card_id, source, target):
        card = self.card(card_id, source)
        self.boards[source]["cards"].remove(card)
        self.boards[target]["cards"].append(card)
        self.act(source, "moveCardFromBoard", card={"id": card_id})
        self.act(target, "moveCardToBoard", card={"id": card_id})
        for action in [a for a in self.actions[source] if a["type"] == "commentCard"]:
            if action["data"]["card"]["id"] == card_id:
                self.actions[source].remove(action)
                self.actions[target].append(action)

    def comment(self, card_id, text, board_id=BOARD, author=SAM):
        action = self.act(board_id, "commentCard", card={"id": card_id}, text=text)
        action["memberCreator"] = author
        return action

    def card(self, card_id, board_id=BOARD):
        return next(c for c in self.boards[board_id]["cards"] if c["id"] == card_id)

    def _comments(self, card_id):
        return [
            a
            for actions in self.actions.values()
            for a in actions
            if a["type"] == "commentCard" and a["data"]["card"]["id"] == card_id
        ]

    def _fail(self, key):
        if key in self.rate_limited:
            raise TrelloRateLimitedError("Trello rate limit exceeded (HTTP 429)")
        status = self.errors.get(key)
        if status == 401:
            raise TrelloAuthError("Trello rejected the API key or token (HTTP 401)")
        if status:
            raise TrelloAPIError(f"Trello request failed: HTTP {status}", status=status)

    def get(self, path, params=None):
        params = dict(params or {})
        self.calls.append((path, params))
        if path == "/members/me":
            if self.bad_token:
                raise TrelloAuthError("Trello rejected the API key or token (HTTP 401)")
            return {"id": "me"}
        if path == "/members/me/boards" or path.startswith("/organizations/"):
            organization = path.split("/")[2] if path.startswith("/organizations/") else None
            return [
                {"id": b["id"]}
                for b in self.boards.values()
                if (organization is None or b["idOrganization"] == organization)
                and (params["filter"] == "all" or not b["closed"])
            ]
        match = re.fullmatch(r"/cards/(\w+)/actions", path)
        if match:
            self._fail(match.group(1))
            comments = sorted(self._comments(match.group(1)), key=lambda a: a["id"], reverse=True)
            page = int(params["page"])
            return comments[page * 50 : (page + 1) * 50]
        match = re.fullmatch(r"/boards/(\w+)(/actions|/checklists)?", path)
        if not match:
            raise AssertionError(f"unexpected path {path}")
        board_id, part = match.groups()
        self._fail(board_id)
        if board_id not in self.boards:
            raise TrelloAPIError("Trello request failed: HTTP 404", status=404)
        board = self.boards[board_id]
        if part == "/checklists":
            return board["checklists"]
        if part is None and "cards" in params:
            cards = [
                {**c, "badges": {"comments": len(self._comments(c["id"]))}} for c in board["cards"]
            ]
            return {"id": board_id, "cards": cards}
        if part is None:
            return {k: v for k, v in board.items() if k not in ("cards", "checklists")}
        actions = sorted(self.actions[board_id], key=lambda a: a["id"], reverse=True)
        if params.get("filter", "all") != "all":
            actions = [a for a in actions if a["type"] in params["filter"].split(",")]
        if params.get("since"):
            actions = [a for a in actions if a["id"] >= params["since"]]
        if params.get("before"):
            actions = [a for a in actions if a["id"] < params["before"]]
        return actions[: params["limit"]]


@pytest.fixture
def trello():
    return FakeTrello()
