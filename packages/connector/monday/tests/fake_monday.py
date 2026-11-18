"""An in-memory monday.com that answers the connector's queries.

It stands in for ``_MondayClient`` (same ``query`` and ``paginate`` methods) and
mimics what a real account showed: posting an update does not move the item's
``updated_at``, sub-items live on their own board, an unknown board comes back
as an empty list, and ``items(ids)`` also returns archived and deleted items.
"""

import itertools
import json
import re

_clock = itertools.count(1)


def _now() -> str:
    return f"2026-10-17T10:{next(_clock):05d}Z"


class FakeMonday:
    def __init__(self, account_id="1"):
        self.account_id = account_id
        self.boards: dict[str, dict] = {}
        self.items: dict[str, dict] = {}
        self.calls: list[str] = []

    # -- building a fake account -------------------------------------------
    def add_board(self, board_id, name, subitems_board=None):
        columns = [{"id": "name", "title": "Name", "type": "name", "settings_str": "{}"}]
        if subitems_board:
            settings = json.dumps({"boardIds": [int(subitems_board)]})
            columns.append(
                {
                    "id": "subitems",
                    "title": "Subitems",
                    "type": "subtasks",
                    "settings_str": settings,
                }
            )
        columns.append({"id": "status", "title": "Status", "type": "status", "settings_str": "{}"})
        self.boards[board_id] = {
            "id": board_id,
            "name": name,
            # Like monday.com, which names and types sub-item boards this way.
            "type": "sub_items_board" if name.startswith("Subitems of") else "board",
            "state": "active",
            "workspace": {"name": "Main workspace"},
            "columns": columns,
        }

    def add_item(self, item_id, board_id, name, status="", parent=None):
        self.items[item_id] = {
            "id": item_id,
            "name": name,
            "board_id": board_id,
            "state": "active",
            "status": status,
            "parent": parent,
            "updated_at": _now(),
            "updates": [],
        }

    def set_status(self, item_id, status):
        self.items[item_id].update(status=status, updated_at=_now())

    def post_update(self, item_id, text, update_id=None):
        """Like monday.com: the item's updated_at does not move."""
        update = {"id": update_id or f"u{next(_clock)}", "text": text, "at": _now(), "replies": []}
        self.items[item_id]["updates"].append(update)
        return update

    def edit_update(self, item_id, update_id, text):
        for update in self.items[item_id]["updates"]:
            if update["id"] == update_id:
                update.update(text=text, at=_now())

    def reply(self, item_id, update_id, text):
        for update in self.items[item_id]["updates"]:
            if update["id"] == update_id:
                update["replies"].append({"id": f"r{next(_clock)}", "text": text, "at": _now()})

    def archive(self, item_id):
        self.items[item_id]["state"] = "archived"

    def delete(self, item_id):
        """Deleted items are still returned by id, with state "deleted"."""
        self.items[item_id]["state"] = "deleted"

    def purge(self, item_id):
        """Gone for good, e.g. emptied from the trash: not returned at all."""
        del self.items[item_id]

    def move(self, item_id, board_id):
        self.items[item_id].update(board_id=board_id, updated_at=_now())

    def set_board_state(self, board_id, state):
        """Archived and deleted boards are only answered with state: all."""
        self.boards[board_id]["state"] = state

    def revoke_access(self, board_id):
        """A board the token cannot see is simply left out of the answer."""
        del self.boards[board_id]

    # -- the client interface ----------------------------------------------
    def query(self, query, variables=None):
        name = _operation(query)
        self.calls.append(name)
        variables = variables or {}
        if name == "Account":
            return {"account": {"id": self.account_id}}
        if name == "Boards":
            assert "state: all" in query
            return {"boards": [self.boards[i] for i in variables["ids"] if i in self.boards]}
        if name == "ListBoards":
            boards = [b for b in self.boards.values() if b["state"] == "active"]
            start = (variables["page"] - 1) * variables["limit"]
            return {"boards": boards[start : start + variables["limit"]]}
        if name in ("ItemDetails", "ItemStates"):
            ids = variables["ids"]
            assert len(ids) <= variables["limit"]
            return {"items": [self._details(i) for i in ids if i in self.items]}
        raise AssertionError(f"unexpected query {name}")

    def paginate(self, query, variables, cursor_path, items_path):
        name = _operation(query)
        self.calls.append(name)
        assert name == "ItemStamps"
        board_id = variables["board"][0]
        for item in self._active(board_id):
            yield {
                "id": item["id"],
                "updated_at": item["updated_at"],
                "updates": [
                    {
                        "id": u["id"],
                        "updated_at": u["at"],
                        "replies": [{"id": r["id"], "updated_at": r["at"]} for r in u["replies"]],
                    }
                    for u in item["updates"]
                ],
            }

    # -- helpers -----------------------------------------------------------
    def _active(self, board_id):
        return [
            i for i in self.items.values() if i["board_id"] == board_id and i["state"] == "active"
        ]

    def _details(self, item_id):
        item = self.items[item_id]
        parent = self.items.get(item["parent"]) if item["parent"] else None
        return {
            "id": item_id,
            "name": item["name"],
            "url": f"https://acme.monday.com/boards/{item['board_id']}/pulses/{item_id}",
            "state": item["state"],
            "board": {"id": item["board_id"]},
            "group": {"title": "Group"},
            "parent_item": {"name": parent["name"]} if parent else None,
            "description": None,
            "column_values": [{"id": "status", "type": "status", "text": item["status"]}],
            "updates": [
                {
                    "id": u["id"],
                    "text_body": u["text"],
                    "created_at": "2026-10-17T09:00:00Z",
                    "creator": {"name": "Alex Doe"},
                    "replies": [
                        {
                            "id": r["id"],
                            "text_body": r["text"],
                            "created_at": "2026-10-17T09:30:00Z",
                            "creator": {"name": "Sam Roe"},
                        }
                        for r in u["replies"]
                    ],
                }
                for u in item["updates"]
            ],
        }


def _operation(query: str) -> str:
    return re.search(r"query\s+(\w+)", query).group(1)
