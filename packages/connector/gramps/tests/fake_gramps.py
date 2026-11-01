"""An in-memory Gramps Web API, served through a fake ``requests`` session.

It answers the endpoints the connector uses, the way Gramps Web 3.x does: token login
and refresh, ``/metadata/``, paged object listings with ``X-Total-Count`` and the
``change > N`` GQL filter, single objects by handle (404 when gone) and the
transaction history. Edits bump ``change`` and are logged as transactions, and
failures (429, 500, 401, a network error) can be queued per endpoint.
"""

from __future__ import annotations

import copy
import re
from collections import defaultdict
from urllib.parse import urlparse

import requests

BASE_URL = "https://gramps.test"
USERNAME = "owner"
PASSWORD = "s3cret-password"

_KINDS = ("people", "families", "events", "places", "notes", "tags")
_CLASS = {
    "people": "Person",
    "families": "Family",
    "events": "Event",
    "places": "Place",
    "notes": "Note",
    "tags": "Tag",
}


class FakeResponse:
    def __init__(self, status_code: int, body=None, headers: dict | None = None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no JSON")
        return self._body


def date(year: int, month: int = 0, day: int = 0, modifier: int = 0, quality: int = 0) -> dict:
    return {
        "_class": "Date",
        "dateval": [day, month, year, False],
        "modifier": modifier,
        "quality": quality,
        "text": "",
        "sortval": 0,
    }


class FakeGramps:
    """One tree. ``session`` is what the connector's client sends its requests to."""

    def __init__(self, tree_id: str = "tree0001", history: bool = True):
        self.tree_id = tree_id
        self.objects: dict[str, dict[str, dict]] = {kind: {} for kind in _KINDS}
        self.clock = 1_700_000_000
        self.transactions: list[dict] = []
        self.history_enabled = history
        self.failures: dict[str, list] = defaultdict(list)
        self.calls: list[tuple[str, str, dict]] = []
        self.tokens_issued = 0
        self.valid_tokens: set[str] = set()
        self.refresh_tokens: set[str] = set()
        self.session = self
        self._ids = defaultdict(int)

    # -- the requests.Session surface -------------------------------------
    def request(self, method, url, headers=None, timeout=None, params=None, json=None):
        path = urlparse(url).path
        assert path.startswith("/api/"), path
        path = path[len("/api") :]
        self.calls.append((method, path, dict(params or {})))
        queued = self.failures.get(path) or self.failures.get("*")
        if queued:
            failure = queued.pop(0)
            if isinstance(failure, Exception):
                raise failure
            status, extra = failure if isinstance(failure, tuple) else (failure, {})
            return FakeResponse(status, {"error": "injected"}, extra)
        token = ((headers or {}).get("Authorization") or "").removeprefix("Bearer ")
        if method == "POST" and path == "/token/":
            if json != {"username": USERNAME, "password": PASSWORD}:
                return FakeResponse(403, {"error": "bad credentials"})
            return FakeResponse(200, self._issue())
        if method == "POST" and path == "/token/refresh/":
            if token not in self.refresh_tokens:
                return FakeResponse(401, {"error": "bad refresh"})
            body = self._issue()
            return FakeResponse(200, {"access_token": body["access_token"]})
        if token not in self.valid_tokens:
            return FakeResponse(401, {"error": "token expired"})
        return self._get(path, dict(params or {}))

    def _issue(self) -> dict:
        self.tokens_issued += 1
        access, refresh = f"access-{self.tokens_issued}", f"refresh-{self.tokens_issued}"
        self.valid_tokens.add(access)
        self.refresh_tokens.add(refresh)
        return {"access_token": access, "refresh_token": refresh}

    def expire_tokens(self) -> None:
        self.valid_tokens.clear()

    def _get(self, path: str, params: dict) -> FakeResponse:
        if path == "/metadata/":
            counts = {kind: len(objs) for kind, objs in self.objects.items()}
            return FakeResponse(200, {"database": {"id": self.tree_id}, "object_counts": counts})
        if path == "/transactions/history/":
            return self._history(params)
        parts = [p for p in path.split("/") if p]
        if not parts or parts[0] not in self.objects:
            return FakeResponse(404, {"error": "no such endpoint"})
        objs = self.objects[parts[0]]
        if len(parts) == 2:
            obj = objs.get(parts[1])
            return FakeResponse(200, copy.deepcopy(obj)) if obj else FakeResponse(404, {})
        items = list(objs.values())
        gql = params.get("gql")
        if gql:
            match = re.fullmatch(r"change (>=|>) (\d+)", gql)
            assert match, gql
            limit = int(match.group(2))
            if match.group(1) == ">":
                items = [o for o in items if o["change"] > limit]
            else:
                items = [o for o in items if o["change"] >= limit]
        return self._page(items, params)

    @staticmethod
    def _page(items: list, params: dict) -> FakeResponse:
        total = len(items)
        if "page" in params:
            page, size = int(params["page"]), int(params.get("pagesize", 20))
            items = items[(page - 1) * size : page * size]
        return FakeResponse(200, copy.deepcopy(items), {"X-Total-Count": str(total)})

    def _history(self, params: dict) -> FakeResponse:
        if not self.history_enabled:
            return FakeResponse(403, {"error": "forbidden"})
        items = self.transactions
        if "after_id" in params:
            items = [t for t in items if t["id"] > int(params["after_id"])]
        items = sorted(items, key=lambda t: t["id"], reverse=params.get("sort") == "-id")
        return self._page(items, params)

    # -- editing the tree ---------------------------------------------------
    def _tick(self) -> int:
        self.clock += 1
        return self.clock

    def _log(self, kind: str, handle: str, trans_type: int) -> None:
        self.transactions.append(
            {
                "id": len(self.transactions) + 1,
                "timestamp": self.clock,
                "changes": [
                    {"obj_class": _CLASS[kind], "obj_handle": handle, "trans_type": trans_type}
                ],
            }
        )

    def _put(self, kind: str, obj: dict, log: bool = True) -> dict:
        new = obj["handle"] not in self.objects[kind]
        obj["change"] = self._tick()
        self.objects[kind][obj["handle"]] = obj
        if log:
            self._log(kind, obj["handle"], 0 if new else 1)
        return obj

    def _gramps_id(self, kind: str) -> str:
        self._ids[kind] += 1
        letter = {"people": "I", "families": "F", "events": "E", "places": "P", "notes": "N"}
        return f"{letter.get(kind, 'X')}{self._ids[kind]:04d}"

    def edit(self, kind: str, handle: str, **fields) -> dict:
        obj = self.objects[kind][handle]
        obj.update(fields)
        return self._put(kind, obj)

    def delete(self, kind: str, handle: str, log: bool = True) -> None:
        del self.objects[kind][handle]
        self._tick()
        if log:
            self._log(kind, handle, 2)

    def add_tag(self, handle: str, name: str) -> dict:
        return self._put("tags", {"handle": handle, "name": name})

    def add_note(self, handle: str, text: str, private: bool = False) -> dict:
        return self._put(
            "notes",
            {
                "handle": handle,
                "gramps_id": self._gramps_id("notes"),
                "private": private,
                "text": {"_class": "StyledText", "string": text, "tags": []},
            },
        )

    def add_place(self, handle: str, name: str, parent: str | None = None, **extra) -> dict:
        place = {
            "handle": handle,
            "gramps_id": self._gramps_id("places"),
            "private": False,
            "name": {"_class": "PlaceName", "value": name},
            "alt_names": [],
            "title": "",
            "place_type": "City",
            "placeref_list": [{"ref": parent}] if parent else [],
            "lat": "",
            "long": "",
            "note_list": [],
            "tag_list": [],
        }
        place.update(extra)
        return self._put("places", place)

    def add_event(self, handle: str, type_: str, when: dict | None = None, place=None, **extra):
        event = {
            "handle": handle,
            "gramps_id": self._gramps_id("events"),
            "private": False,
            "type": type_,
            "date": when or {"dateval": [0, 0, 0, False], "modifier": 0, "quality": 0},
            "place": place or "",
            "description": "",
            "note_list": [],
        }
        event.update(extra)
        return self._put("events", event)

    def add_person(
        self,
        handle: str,
        first: str,
        surname: str,
        gender: int = 2,
        events: list | None = None,
        birth: int = -1,
        death: int = -1,
        **extra,
    ) -> dict:
        person = {
            "handle": handle,
            "gramps_id": self._gramps_id("people"),
            "private": False,
            "gender": gender,
            "primary_name": {
                "first_name": first,
                "surname_list": [{"prefix": "", "surname": surname}],
                "suffix": "",
                "title": "",
            },
            "alternate_names": [],
            "event_ref_list": [
                {"ref": e, "role": "Primary", "private": False} for e in (events or [])
            ],
            "birth_ref_index": birth,
            "death_ref_index": death,
            "family_list": [],
            "parent_family_list": [],
            "person_ref_list": [],
            "attribute_list": [],
            "note_list": [],
            "tag_list": [],
        }
        person.update(extra)
        return self._put("people", person)

    def add_family(
        self, handle: str, father=None, mother=None, children=(), type_="Married", **extra
    ) -> dict:
        family = {
            "handle": handle,
            "gramps_id": self._gramps_id("families"),
            "private": False,
            "type": type_,
            "father_handle": father,
            "mother_handle": mother,
            "child_ref_list": [
                {"ref": c, "frel": "Birth", "mrel": "Birth", "private": False} for c in children
            ],
            "event_ref_list": [],
            "attribute_list": [],
            "note_list": [],
            "tag_list": [],
        }
        family.update(extra)
        self._put("families", family)
        for parent in (father, mother):
            if parent:
                self.objects["people"][parent]["family_list"].append(handle)
        for child in children:
            self.objects["people"][child]["parent_family_list"].append(handle)
        return family

    def fail(self, path: str, *failures) -> None:
        """Queue failures for a path (``"*"`` for any): a status, ``(status, headers)``
        or an exception to raise."""
        self.failures[path].extend(failures)

    def gets(self, path_prefix: str) -> int:
        return sum(1 for method, path, _ in self.calls if path.startswith(path_prefix))


NETWORK_ERROR = requests.ConnectionError


def small_tree(fake: FakeGramps | None = None) -> FakeGramps:
    """Two parents, a child, places, events, a note and a tag."""
    fake = fake or FakeGramps()
    fake.add_tag("t1", "Family")
    fake.add_place("pl_eng", "England")
    fake.add_place("pl_lon", "London", parent="pl_eng")
    fake.add_event("e_jb", "Birth", date(1819, 5, 24), place="pl_lon")
    fake.add_event("e_ab", "Birth", date(1819, 8, 26, modifier=3))
    fake.add_event("e_cb", "Birth", date(1840, 11, 21), place="pl_lon")
    fake.add_event("e_cd", "Death", date(1901, 1, 22))
    fake.add_event("e_occ", "Occupation", description="Schoolteacher")
    fake.add_event("e_wed", "Marriage", date(1840, 2, 10), place="pl_lon")
    fake.add_note("n1", "Kept a diary all her life.")
    fake.add_person("p_john", "John", "Smith", gender=1, events=["e_jb"], birth=0)
    fake.add_person("p_anna", "Anna", "Jones", gender=0, events=["e_ab"], birth=0)
    fake.add_person(
        "p_mary",
        "Mary",
        "Smith",
        gender=0,
        events=["e_cb", "e_cd", "e_occ"],
        birth=0,
        death=1,
        note_list=["n1"],
        tag_list=["t1"],
    )
    fake.add_family(
        "f1",
        father="p_john",
        mother="p_anna",
        children=["p_mary"],
        event_ref_list=[{"ref": "e_wed", "role": "Family", "private": False}],
    )
    return fake
