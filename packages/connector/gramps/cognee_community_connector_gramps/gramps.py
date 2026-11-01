"""Gramps Web connector for cognee: a ``dlt`` resource that turns a family tree into memory.

One document per person (with their parents, siblings, partners, children, events,
places and notes written out by name) and one per family, optionally one per place.
Synced incrementally with forget-on-delete. Hand the resource to ``cognee.remember``::

    await cognee.remember(
        gramps_source(base_url="https://gramps.example.org"),  # GRAMPS_* env vars
        dataset_name="family_tree",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

The README describes what is stored, how changes and deletions are found, and what a
run does when Gramps Web fails part of the way through.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Iterable, Iterator
from typing import Any

import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, NODE_SET_COLUMN

logger = get_logger("gramps_connector")

GRAMPS_SOURCE_NAME = "gramps"
# Document kinds a source can emit. Events and notes are not documents of their own:
# they are written into the person, family or place they belong to.
KINDS = ("people", "families", "places")
DEFAULT_INCLUDE = ("people", "families")

# Version of the sync state below; a state of another version starts a fresh full pass.
_STATE_VERSION = 1

# One request, generously bounded, so a stalled connection fails and is retried.
_TIMEOUT_SECONDS = 60
# Rows per listing page. Gramps Web has no hard cap; 500 keeps a page of events small.
_PAGE_SIZE = 500
# Five attempts per request with 1, 2, 4, 8 s between them, or Gramps Web's Retry-After.
_MAX_RETRIES = 5
_TRANSIENT_STATUSES = (429, 500, 502, 503, 504)
# The longest single wait and the most one run waits in all. cognee holds its dlt staging
# lock during a sync, so a longer wait aborts the run instead (nothing is lost or forgotten).
_MAX_WAIT_SECONDS = 60
_MAX_TOTAL_WAIT_SECONDS = 120
# How often a listing is read again when the tree changed under it (its count moved).
_MAX_LISTING_ATTEMPTS = 3
# Listing pages before a listing is treated as broken (5,000,000 objects at 500 a page).
_MAX_LIST_PAGES = 10_000
# Records a run checks one by one before forgetting them, when the transaction history
# cannot confirm the deletion. The rest wait for the next run (which does a full pass).
_MAX_DELETE_CHECKS = 200
# Guard against a place hierarchy that loops.
_MAX_PLACE_DEPTH = 20

# Gramps object types the connector reads, with the API fields each one needs.
_FIELDS = {
    "people": (
        "handle,gramps_id,change,private,gender,primary_name,alternate_names,"
        "event_ref_list,birth_ref_index,death_ref_index,family_list,parent_family_list,"
        "person_ref_list,attribute_list,note_list,tag_list"
    ),
    "families": (
        "handle,gramps_id,change,private,type,father_handle,mother_handle,child_ref_list,"
        "event_ref_list,attribute_list,note_list,tag_list"
    ),
    "events": "handle,gramps_id,change,private,type,date,place,description,note_list",
    "places": (
        "handle,gramps_id,change,private,name,alt_names,title,place_type,placeref_list,"
        "lat,long,note_list,tag_list"
    ),
    "notes": "handle,gramps_id,change,private,text",
    "tags": "handle,name,change",
}
# What each document kind is rendered from.
_NEEDS = {
    "people": ("people", "families", "events", "places", "notes", "tags"),
    "families": ("people", "families", "events", "places", "notes", "tags"),
    "places": ("places", "notes", "tags"),
}
# Row id prefix and Gramps Web page of each kind.
_ROW_PREFIX = {"people": "person", "families": "family", "places": "place"}

# Gramps' enumerations, as stored in the JSON objects.
_GENDER = {0: "female", 1: "male", 2: "unknown", 3: "other"}
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_MOD_BEFORE, _MOD_AFTER, _MOD_ABOUT, _MOD_RANGE, _MOD_SPAN, _MOD_TEXT = 1, 2, 3, 4, 5, 6
_MOD_FROM, _MOD_TO = 7, 8
_QUALITY = {1: "estimated", 2: "calculated"}
# trans_type of a change in /transactions/history/: 0 add, 1 update, 2 delete.
_TXN_DELETE = 2
_TXN_CLASS = {"Person": "people", "Family": "families", "Place": "places"}


class GrampsSourceError(RuntimeError):
    """Base error. Messages name statuses, endpoints and counts, never tree content."""


class GrampsAPIError(GrampsSourceError):
    """An error answer from Gramps Web (``status``), or no answer at all (``status`` 0)."""

    def __init__(self, status: int, endpoint: str, detail: str = ""):
        answer = f"HTTP {status}" if status else "no answer"
        super().__init__(f"Gramps Web {endpoint}: {answer}")
        self.status = status
        self.endpoint = endpoint
        self.detail = detail


class GrampsAuthError(GrampsAPIError):
    """Login failed, or the token was refused and could not be renewed."""


class GrampsListingError(GrampsSourceError):
    """A listing could not be read completely, so nothing may be forgotten."""


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _retry_delay(headers: Any, attempt: int) -> float:
    """Seconds before the next attempt: Gramps Web's Retry-After, else 1, 2, 4, 8 s."""
    value = (headers or {}).get("Retry-After")
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return float(2**attempt)


class GrampsClient:
    """Access to one Gramps Web tree: login, token renewal, retries and waits.

    Logs in lazily with ``POST /api/token/`` and keeps the access and refresh tokens
    in memory only, for as long as the client lives, so reusing a client across runs
    does not log in again (Gramps Web rate-limits logins). A refused token is renewed
    with the refresh token, then by logging in again. Credentials and tokens never
    reach a message, a log line or ``repr``.
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        session: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not base_url or not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must be the http(s) address of a Gramps Web server")
        if not username or not password:
            raise ValueError("Gramps Web needs a username and a password")
        self.base_url = base_url.rstrip("/").removesuffix("/api")
        self._username = username
        self._password = password
        self._session = session if session is not None else requests.Session()
        self._sleep = sleep
        self._access: str | None = None
        self._refresh: str | None = None
        self.check_active: Callable[[], None] | None = None
        self.waited = 0.0
        self.requests = 0
        self.logins = 0

    def __repr__(self) -> str:
        return f"GrampsClient(base_url={self.base_url!r})"

    def start_run(self, check_active: Callable[[], None] | None) -> None:
        """Reset the per-run wait budget (tokens are kept)."""
        self.check_active = check_active
        self.waited = 0.0
        self.requests = 0

    def _check(self) -> None:
        if self.check_active is not None:
            self.check_active()

    def _wait(self, seconds: float, endpoint: str, status: int) -> None:
        if seconds > _MAX_WAIT_SECONDS or self.waited + seconds > _MAX_TOTAL_WAIT_SECONDS:
            raise GrampsAPIError(status, endpoint, "retry wait over the run's budget")
        self._check()
        self.waited += seconds
        self._sleep(seconds)

    def _send(self, method: str, path: str, *, token: str | None, **kwargs: Any) -> Any:
        """One request with retries on 429, 5xx and network errors; returns the response."""
        url = f"{self.base_url}/api{path}"
        endpoint = path.split("?")[0]
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        for attempt in range(_MAX_RETRIES):
            last = attempt == _MAX_RETRIES - 1
            self._check()
            self.requests += 1
            try:
                response = self._session.request(
                    method, url, headers=headers, timeout=_TIMEOUT_SECONDS, **kwargs
                )
            except requests.RequestException as exc:
                # only the type: some requests errors carry headers in their message
                if last:
                    raise GrampsAPIError(0, endpoint, type(exc).__name__) from None
                logger.warning("Gramps Web: %s on %s; retrying.", type(exc).__name__, endpoint)
                self._wait(float(2**attempt), endpoint, 0)
                continue
            if response.status_code in _TRANSIENT_STATUSES and not last:
                logger.warning(
                    "Gramps Web: HTTP %d on %s; retrying.", response.status_code, endpoint
                )
                self._wait(_retry_delay(response.headers, attempt), endpoint, response.status_code)
                continue
            return response
        raise AssertionError("unreachable")  # pragma: no cover

    def _login(self) -> None:
        self.logins += 1
        response = self._send(
            "POST",
            "/token/",
            token=None,
            json={"username": self._username, "password": self._password},
        )
        if response.status_code != 200:
            raise GrampsAuthError(response.status_code, "/token/", "login refused")
        body = _json(response, "/token/")
        if not isinstance(body, dict) or not body.get("access_token"):
            raise GrampsAuthError(response.status_code, "/token/", "no access token")
        self._access = body["access_token"]
        self._refresh = body.get("refresh_token")

    def _renew(self) -> None:
        """New access token from the refresh token, else a fresh login."""
        if self._refresh:
            response = self._send("POST", "/token/refresh/", token=self._refresh)
            body = _json(response, "/token/refresh/") if response.status_code == 200 else None
            if isinstance(body, dict) and body.get("access_token"):
                self._access = body["access_token"]
                return
        self._access = self._refresh = None
        self._login()

    def get(
        self, path: str, params: dict | None = None, ok: tuple[int, ...] = (200,)
    ) -> tuple[int, Any, Any]:
        """GET an API path; returns ``(status, json, headers)``. Other statuses raise."""
        if self._access is None:
            self._login()
        response = self._send("GET", path, token=self._access, params=params or {})
        if response.status_code == 401:
            self._renew()
            response = self._send("GET", path, token=self._access, params=params or {})
            if response.status_code == 401:
                raise GrampsAuthError(401, path, "token refused after renewal")
        if response.status_code not in ok:
            raise GrampsAPIError(response.status_code, path)
        body = _json(response, path) if response.status_code == 200 else None
        return response.status_code, body, response.headers


def _json(response: Any, endpoint: str) -> Any:
    try:
        return response.json()
    except ValueError:
        raise GrampsAPIError(response.status_code, endpoint, "answer is not JSON") from None


def _total(headers: Any) -> int | None:
    try:
        return int((headers or {}).get("X-Total-Count"))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Reading the tree
# ---------------------------------------------------------------------------
def _list_once(client: GrampsClient, kind: str) -> tuple[list[dict], bool]:
    """Read every object of one type, page by page. Returns ``(objects, consistent)``.

    Consistent means the total did not move between the first and the last page and
    the pages held exactly that many distinct handles; otherwise an edit shifted the
    pages and an object may have been skipped.
    """
    objects: dict[str, dict] = {}
    first_total: int | None = None
    total: int | None = None
    for page in range(1, _MAX_LIST_PAGES + 1):
        _, body, headers = client.get(
            f"/{kind}/", {"keys": _FIELDS[kind], "page": page, "pagesize": _PAGE_SIZE}
        )
        if not isinstance(body, list):
            raise GrampsListingError(f"Gramps Web /{kind}/ did not answer with a list")
        total = _total(headers)
        if first_total is None:
            first_total = total
        for obj in body:
            if not isinstance(obj, dict) or not isinstance(obj.get("handle"), str):
                raise GrampsListingError(f"Gramps Web /{kind}/ sent an object without a handle")
            objects[obj["handle"]] = obj
        if len(body) < _PAGE_SIZE or (total is not None and page * _PAGE_SIZE >= total):
            break
    else:
        raise GrampsListingError(f"Gramps Web /{kind}/ did not end after {_MAX_LIST_PAGES} pages")
    consistent = total is None or (first_total == total and len(objects) == total)
    return list(objects.values()), consistent


def _list_all(client: GrampsClient, kind: str) -> list[dict]:
    for attempt in range(_MAX_LISTING_ATTEMPTS):
        objects, consistent = _list_once(client, kind)
        if consistent:
            return objects
        logger.warning(
            "Gramps Web: /%s/ changed while it was read; reading it again (%d/%d).",
            kind,
            attempt + 1,
            _MAX_LISTING_ATTEMPTS,
        )
    raise GrampsListingError(
        f"Gramps Web /{kind}/ kept changing while it was read; nothing was synced or forgotten"
    )


def _count(client: GrampsClient, kind: str, gql: str | None = None) -> int:
    """How many objects of a type there are (matching ``gql``), from one tiny request."""
    params: dict[str, Any] = {"keys": "handle", "page": 1, "pagesize": 1}
    if gql:
        params["gql"] = gql
    _, _, headers = client.get(f"/{kind}/", params)
    total = _total(headers)
    if total is None:
        raise GrampsListingError(f"Gramps Web /{kind}/ sent no X-Total-Count")
    return total


def _marks(objects: list[dict]) -> dict[str, int]:
    """What the quick check compares: how many objects there are and the newest change."""
    changes = [int(obj.get("change") or 0) for obj in objects]
    return {"count": len(objects), "cursor": max(changes, default=0)}


def _tree_changed(client: GrampsClient, marks: dict[str, dict]) -> bool:
    """True when any type gained, lost or changed an object since ``marks`` were taken.

    Two requests per type: the total (a deletion lowers it) and the objects changed
    after the newest change seen (an add or an edit bumps ``change`` to its time).
    """
    for kind, mark in marks.items():
        if _count(client, kind) != mark["count"]:
            return True
        if _count(client, kind, f"change > {mark['cursor']}"):
            return True
    return False


def _history_head(client: GrampsClient) -> int | None:
    """The id of the newest transaction, or ``None`` when the history is not readable."""
    try:
        _, body, _ = client.get(
            "/transactions/history/", {"sort": "-id", "page": 1, "pagesize": 1}, ok=(200,)
        )
    except GrampsAPIError as exc:
        if exc.status in (403, 404, 422):
            return None
        raise
    if not isinstance(body, list):
        return None
    return int(body[0]["id"]) if body else 0


def _history_deletes(client: GrampsClient, after_id: int) -> tuple[set[str], int] | None:
    """Row ids deleted in transactions after ``after_id`` and the newest id seen.

    ``None`` when the history is not readable for this user (it needs the permission to
    view private records); deletions are then checked one by one.
    """
    deleted: set[str] = set()
    head = after_id
    for page in range(1, _MAX_LIST_PAGES + 1):
        try:
            _, body, headers = client.get(
                "/transactions/history/",
                {"after_id": after_id, "sort": "id", "page": page, "pagesize": 100},
            )
        except GrampsAPIError as exc:
            if exc.status in (403, 404, 422):
                return None
            raise
        if not isinstance(body, list):
            return None
        for txn in body:
            head = max(head, int(txn.get("id") or 0))
            for change in txn.get("changes") or []:
                kind = _TXN_CLASS.get(change.get("obj_class"))
                if kind and change.get("trans_type") == _TXN_DELETE and change.get("obj_handle"):
                    deleted.add(_row_id(kind, change["obj_handle"]))
        total = _total(headers)
        if len(body) < 100 or (total is not None and page * 100 >= total):
            return deleted, head
    raise GrampsListingError("Gramps Web /transactions/history/ did not end")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _row_id(kind: str, handle: str) -> str:
    return f"gramps:{_ROW_PREFIX[kind]}:{handle}"


def _one_date(day: int, month: int, year: int) -> str:
    parts = []
    if day:
        parts.append(str(day))
    if 1 <= month <= 12:
        parts.append(_MONTHS[month - 1])
    if year:
        parts.append(str(year))
    return " ".join(parts)


def format_date(date: Any) -> str:
    """A Gramps date object as text: ``24 May 1819``, ``about 1820``, ``between 1800 and 1810``."""
    if not isinstance(date, dict):
        return ""
    text = str(date.get("text") or "").strip()
    modifier = date.get("modifier") or 0
    value = list(date.get("dateval") or [])
    if modifier == _MOD_TEXT or len(value) < 3:
        return text
    first = _one_date(*(int(v or 0) for v in value[:3]))
    if not first:
        return text
    if modifier in (_MOD_RANGE, _MOD_SPAN) and len(value) >= 7:
        second = _one_date(*(int(v or 0) for v in value[4:7]))
        word = ("between", "and") if modifier == _MOD_RANGE else ("from", "to")
        rendered = f"{word[0]} {first} {word[1]} {second}" if second else first
    else:
        prefix = {
            _MOD_BEFORE: "before",
            _MOD_AFTER: "after",
            _MOD_ABOUT: "about",
            _MOD_FROM: "from",
            _MOD_TO: "to",
        }.get(modifier)
        rendered = f"{prefix} {first}" if prefix else first
    quality = _QUALITY.get(date.get("quality") or 0)
    return f"{quality} {rendered}" if quality else rendered


def format_name(name: Any) -> str:
    """A Gramps name object as ``Title Given Surname(s), Suffix``."""
    if not isinstance(name, dict):
        return ""
    surnames = " ".join(
        " ".join(p for p in (s.get("prefix"), s.get("surname")) if p)
        for s in name.get("surname_list") or []
        if isinstance(s, dict) and (s.get("surname") or s.get("prefix"))
    )
    full = " ".join(p for p in (name.get("title"), name.get("first_name"), surnames) if p)
    suffix = name.get("suffix")
    if suffix:
        full = f"{full}, {suffix}" if full else suffix
    return full.strip()


def _type_name(value: Any) -> str:
    """A Gramps type (event type, family relationship, ...) as text; custom ones are strings."""
    if isinstance(value, dict):
        value = value.get("string") or value.get("value") or ""
    return str(value or "").strip()


def _internal(type_name: str) -> bool:
    """GEDCOM bookkeeping such as ``_UID`` or ``_UPD``: not facts about anyone."""
    return type_name.startswith("_")


class _Tree:
    """The objects of one run, by handle, and how to write them out by name."""

    def __init__(self, objects: dict[str, list[dict]], include_private: bool, base_url: str):
        self.by_kind = {kind: {o["handle"]: o for o in objs} for kind, objs in objects.items()}
        self.include_private = include_private
        self.base_url = base_url
        self.tag_names = {
            handle: str(tag.get("name") or "").strip()
            for handle, tag in self.by_kind.get("tags", {}).items()
        }

    # -- lookups -------------------------------------------------------------
    def visible(self, obj: Any) -> bool:
        return isinstance(obj, dict) and (self.include_private or not obj.get("private"))

    def get(self, kind: str, handle: Any) -> dict | None:
        obj = self.by_kind.get(kind, {}).get(handle) if isinstance(handle, str) else None
        return obj if self.visible(obj) else None

    def refs(self, items: Any) -> list[dict]:
        """Reference entries (event refs, child refs, ...) this run may show."""
        return [r for r in items or [] if isinstance(r, dict) and self.visible(r)]

    def tags(self, obj: dict) -> list[str]:
        return sorted({n for h in obj.get("tag_list") or [] if (n := self.tag_names.get(h))})

    def person_name(self, handle: Any) -> str:
        person = self.get("people", handle)
        if person is None:
            return ""
        name = format_name(person.get("primary_name")) or f"Unnamed person {person['gramps_id']}"
        birth = self.vital(person, "birth_ref_index")
        year = _year((birth or {}).get("date"))
        return f"{name} (born {year})" if year else name

    def place_title(self, handle: Any) -> str:
        """The place with its enclosing places: ``Kensington, London, England``."""
        names: list[str] = []
        seen: set[str] = set()
        place = self.get("places", handle)
        while place is not None and place["handle"] not in seen and len(names) < _MAX_PLACE_DEPTH:
            seen.add(place["handle"])
            name = str((place.get("name") or {}).get("value") or "").strip()
            if name:
                names.append(name)
            parents = place.get("placeref_list") or []
            place = self.get("places", parents[0].get("ref")) if parents else None
        return ", ".join(names)

    def vital(self, person: dict, index_key: str) -> dict | None:
        """The birth or death event of a person, when it is set and visible."""
        index = person.get(index_key)
        refs = person.get("event_ref_list") or []
        if not isinstance(index, int) or not 0 <= index < len(refs):
            return None
        ref = refs[index]
        if not self.visible(ref):
            return None
        return self.get("events", ref.get("ref"))

    def event_text(self, event: dict) -> str:
        """``24 May 1819, Kensington, London: description`` (without the type)."""
        parts = [
            p for p in (format_date(event.get("date")), self.place_title(event.get("place"))) if p
        ]
        text = ", ".join(parts)
        description = str(event.get("description") or "").strip()
        if description:
            text = f"{text}: {description}" if text else description
        return text

    def note_texts(self, handles: Any) -> list[str]:
        texts = []
        for handle in handles or []:
            note = self.get("notes", handle)
            text = _note_text(note) if note else ""
            if text:
                texts.append(text)
        return texts

    def url(self, kind: str, obj: dict) -> str:
        return f"{self.base_url}/{_ROW_PREFIX[kind]}/{obj.get('gramps_id', '')}"

    # -- documents ----------------------------------------------------------
    def person_row(self, person: dict) -> dict:
        name = format_name(person.get("primary_name")) or f"Unnamed person {person['gramps_id']}"
        lines = [f"Person: {name}", f"Gramps ID: {person.get('gramps_id', '')}"]
        gender = _GENDER.get(person.get("gender"))
        if gender and gender != "unknown":
            lines.append(f"Gender: {gender}")
        aliases = [
            n
            for alt in self.refs(person.get("alternate_names"))
            if (n := format_name(alt)) and n != name
        ]
        if aliases:
            lines.append(f"Also known as: {'; '.join(aliases)}")
        notes: list[str] = list(self.note_texts(person.get("note_list")))
        vital_refs = set()
        for label, key in (("Born", "birth_ref_index"), ("Died", "death_ref_index")):
            event = self.vital(person, key)
            if event is not None:
                vital_refs.add(event["handle"])
                text = self.event_text(event)
                if text:
                    lines.append(f"{label}: {text}")

        for family_handle in person.get("parent_family_list") or []:
            family = self.get("families", family_handle)
            if family is None:
                continue
            own_ref = next(
                (r for r in family.get("child_ref_list") or [] if r.get("ref") == person["handle"]),
                {},
            )
            parents = []
            for role, key, rel_key in (
                ("father", "father_handle", "frel"),
                ("mother", "mother_handle", "mrel"),
            ):
                parent = self.person_name(family.get(key))
                if parent:
                    relation = _type_name(own_ref.get(rel_key))
                    note = f", {relation.lower()}" if relation and relation != "Birth" else ""
                    parents.append(f"{parent} ({role}{note})")
            if parents:
                lines.append(f"Parents: {'; '.join(parents)}")
            siblings = [
                n
                for r in self.refs(family.get("child_ref_list"))
                if r.get("ref") != person["handle"] and (n := self.person_name(r.get("ref")))
            ]
            if siblings:
                lines.append(f"Siblings: {'; '.join(siblings)}")

        for family_handle in person.get("family_list") or []:
            family = self.get("families", family_handle)
            if family is None:
                continue
            partner_key = (
                "mother_handle"
                if family.get("father_handle") == person["handle"]
                else "father_handle"
            )
            partner = self.person_name(family.get(partner_key))
            relationship = _type_name(family.get("type"))
            if partner:
                detail = (
                    f" ({relationship.lower()})"
                    if relationship and relationship != "Unknown"
                    else ""
                )
                lines.append(f"Partner: {partner}{detail}")
            children = [
                n
                for r in self.refs(family.get("child_ref_list"))
                if (n := self.person_name(r.get("ref")))
            ]
            if children:
                with_whom = f" with {partner}" if partner else ""
                lines.append(f"Children{with_whom}: {'; '.join(children)}")

        events = []
        for ref in self.refs(person.get("event_ref_list")):
            event = self.get("events", ref.get("ref"))
            if event is None:
                continue
            kind = _type_name(event.get("type"))
            if _internal(kind):
                continue
            for text in self.note_texts(event.get("note_list")):
                notes.append(f"({kind or 'Event'}) {text}")
            if event["handle"] in vital_refs:
                continue
            role = _type_name(ref.get("role"))
            role_text = f" (as {role.lower()})" if role and role != "Primary" else ""
            text = self.event_text(event)
            events.append(
                f"- {kind or 'Event'}{role_text}: {text}" if text else f"- {kind}{role_text}"
            )
        if events:
            lines.append("Events:")
            lines.extend(events)

        associations = [
            f"- {rel}: {n}" if (rel := str(r.get("rel") or "").strip()) else f"- {n}"
            for r in self.refs(person.get("person_ref_list"))
            if (n := self.person_name(r.get("ref")))
        ]
        if associations:
            lines.append("Associations:")
            lines.extend(associations)
        lines.extend(self._attributes(person))
        return self._row("people", person, name, lines, notes)

    def family_row(self, family: dict) -> dict:
        father = self.person_name(family.get("father_handle"))
        mother = self.person_name(family.get("mother_handle"))
        couple = " and ".join(p for p in (father, mother) if p)
        title = f"Family of {couple}" if couple else f"Family {family.get('gramps_id', '')}"
        lines = [
            f"Family: {title.removeprefix('Family of ')}",
            f"Gramps ID: {family.get('gramps_id', '')}",
        ]
        relationship = _type_name(family.get("type"))
        if relationship and relationship != "Unknown":
            lines.append(f"Relationship: {relationship}")
        if father:
            lines.append(f"Father: {father}")
        if mother:
            lines.append(f"Mother: {mother}")
        children = []
        for ref in self.refs(family.get("child_ref_list")):
            child = self.person_name(ref.get("ref"))
            if not child:
                continue
            relations = {_type_name(ref.get("frel")), _type_name(ref.get("mrel"))} - {"", "Birth"}
            extra = f" ({', '.join(sorted(r.lower() for r in relations))})" if relations else ""
            children.append(f"- {child}{extra}")
        if children:
            lines.append("Children:")
            lines.extend(children)
        notes = list(self.note_texts(family.get("note_list")))
        events = []
        for ref in self.refs(family.get("event_ref_list")):
            event = self.get("events", ref.get("ref"))
            if event is None:
                continue
            kind = _type_name(event.get("type"))
            if _internal(kind):
                continue
            text = self.event_text(event)
            events.append(f"- {kind or 'Event'}: {text}" if text else f"- {kind}")
            for note in self.note_texts(event.get("note_list")):
                notes.append(f"({kind or 'Event'}) {note}")
        if events:
            lines.append("Events:")
            lines.extend(events)
        lines.extend(self._attributes(family))
        return self._row("families", family, title, lines, notes)

    def place_row(self, place: dict) -> dict:
        name = str((place.get("name") or {}).get("value") or "").strip()
        name = name or f"Place {place.get('gramps_id', '')}"
        lines = [f"Place: {name}", f"Gramps ID: {place.get('gramps_id', '')}"]
        place_type = _type_name(place.get("place_type"))
        if place_type and place_type != "Unknown":
            lines.append(f"Type: {place_type}")
        full = self.place_title(place["handle"])
        if full and full != name:
            lines.append(f"Full name: {full}")
        parents = [
            t for r in place.get("placeref_list") or [] if (t := self.place_title(r.get("ref")))
        ]
        if parents:
            lines.append(f"Part of: {'; '.join(parents)}")
        aliases = [
            v for alt in place.get("alt_names") or [] if (v := str(alt.get("value") or "").strip())
        ]
        if aliases:
            lines.append(f"Also known as: {'; '.join(aliases)}")
        if place.get("lat") and place.get("long"):
            lines.append(f"Coordinates: {place['lat']}, {place['long']}")
        return self._row("places", place, name, lines, self.note_texts(place.get("note_list")))

    def _attributes(self, obj: dict) -> list[str]:
        attributes = [
            f"- {kind}: {value}"
            for a in self.refs(obj.get("attribute_list"))
            if (kind := _type_name(a.get("type")))
            and not _internal(kind)
            and (value := str(a.get("value") or "").strip())
        ]
        return ["Attributes:", *attributes] if attributes else []

    def _row(self, kind: str, obj: dict, title: str, lines: list[str], notes: list[str]) -> dict:
        tags = self.tags(obj)
        if tags:
            lines.append(f"Tags: {', '.join(tags)}")
        if notes:
            lines.append("Notes:")
            lines.extend(notes)
        row: dict[str, Any] = {
            "id": _row_id(kind, obj["handle"]),
            "title": title,
            "content": "\n".join(lines),
            "url": self.url(kind, obj),
            NODE_SET_COLUMN: [f"gramps:{kind}", *(f"gramps:tag:{t}" for t in tags)],
            "_deleted": False,
        }
        return row


def _year(date: Any) -> int | None:
    if not isinstance(date, dict) or date.get("modifier") == _MOD_TEXT:
        return None
    value = date.get("dateval") or []
    year = int(value[2] or 0) if len(value) >= 3 else 0
    return year or None


def _note_text(note: dict) -> str:
    text = note.get("text")
    if isinstance(text, dict):
        text = text.get("string")
    return str(text or "").strip()


def _fingerprint(row: dict) -> str:
    return hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# One run
# ---------------------------------------------------------------------------
class _Run:
    def __init__(
        self,
        client: GrampsClient,
        state: dict,
        stats: dict[str, int],
        *,
        include: tuple[str, ...],
        tags: tuple[str, ...] | None,
        include_private: bool,
        full_sync_hours: float,
        now: Callable[[], float] = time.time,
    ):
        self.client = client
        self.state = state
        self.stats = stats
        self.include = include
        self.tags = tags
        self.include_private = include_private
        self.full_sync_seconds = full_sync_hours * 3600
        self.now = now
        self.config = _fingerprint(
            {"include": sorted(include), "tags": sorted(tags or []), "private": include_private}
        )
        # Newest transaction id seen this run; the next run reads deletions after it.
        self._txn_after: int | None = None

    def _needed(self) -> list[str]:
        return sorted({kind for doc in self.include for kind in _NEEDS[doc]})

    def _fresh_state(self) -> bool:
        return self.state.get("v") == _STATE_VERSION

    def _quick_check_ok(self) -> bool:
        """Whether this run may skip the full pass: same tree, same config, nothing changed."""
        if not self._fresh_state() or self.state.get("config") != self.config:
            return False
        if self.state.get("pending_checks"):
            return False
        if self.now() - float(self.state.get("last_full") or 0) >= self.full_sync_seconds:
            return False
        marks = self.state.get("marks") or {}
        if sorted(marks) != self._needed():
            return False
        return not _tree_changed(self.client, marks)

    def _check_tree(self) -> str:
        _, body, _ = self.client.get("/metadata/")
        tree_id = str(((body or {}).get("database") or {}).get("id") or "")
        known = self.state.get("tree_id")
        if known and tree_id and known != tree_id:
            raise GrampsSourceError(
                "This resource_name was synced from another Gramps tree; give each tree its "
                "own resource_name. Nothing was synced or forgotten."
            )
        return tree_id

    def rows(self) -> Iterator[dict]:
        self.stats.update(scanned=0, synced=0, skipped=0, deleted=0, kept=0, failed=0, full=0)
        tree_id = self._check_tree()
        if self._quick_check_ok():
            logger.info("Gramps Web: nothing changed since the last sync.")
            return
        self.stats["full"] = 1
        objects = {kind: _list_all(self.client, kind) for kind in self._needed()}
        tree = _Tree(objects, self.include_private, self.client.base_url)

        previous: dict[str, str] = (
            dict(self.state.get("members") or {}) if self._fresh_state() else {}
        )
        current: dict[str, str] = {}
        listed: set[str] = set()
        out_rows: list[dict] = []
        render = {"people": tree.person_row, "families": tree.family_row, "places": tree.place_row}
        for kind in self.include:
            for obj in objects[kind]:
                listed.add(_row_id(kind, obj["handle"]))
                if not self._in_scope(tree, kind, obj):
                    continue
                row = render[kind](obj)
                fingerprint = _fingerprint(row)
                self.stats["scanned"] += 1
                current[row["id"]] = fingerprint
                if previous.get(row["id"]) == fingerprint:
                    self.stats["skipped"] += 1
                    continue
                out_rows.append(row)

        # Gone: deleted in Gramps (vanished from the listing, confirmed below), or still
        # listed but now private, untagged or of a kind no longer included.
        gone = sorted(set(previous) - set(current))
        vanished = [
            row_id
            for row_id in gone
            if row_id not in listed and self._kind_of(row_id) in self.include
        ]
        pending = self._confirm_deleted(vanished)
        for row_id in gone:
            if row_id in pending:
                current[row_id] = previous[row_id]  # kept: not confirmed gone yet
                self.stats["kept"] += 1

        for row in out_rows:
            self.stats["synced"] += 1
            yield row
        for row_id in gone:
            if row_id in pending:
                continue
            self.stats["deleted"] += 1
            yield {"id": row_id, "_deleted": True}

        self.state.clear()
        self.state.update(
            v=_STATE_VERSION,
            tree_id=tree_id,
            config=self.config,
            members=dict(sorted(current.items())),
            marks={kind: _marks(objs) for kind, objs in objects.items()},
            txn_id=self._txn_after,
            last_full=self.now(),
            pending_checks=sorted(pending),
        )
        logger.info(
            "Gramps Web: %d document(s) synced, %d unchanged, %d forgotten, %d kept.",
            self.stats["synced"],
            self.stats["skipped"],
            self.stats["deleted"],
            self.stats["kept"],
        )

    @staticmethod
    def _kind_of(row_id: str) -> str:
        prefix = row_id.split(":")[1]
        return next(k for k, p in _ROW_PREFIX.items() if p == prefix)

    def _in_scope(self, tree: _Tree, kind: str, obj: dict) -> bool:
        if not tree.visible(obj):
            return False
        if not self.tags:
            return True
        return bool(set(tree.tags(obj)) & set(self.tags))

    def _confirm_deleted(self, vanished: list[str]) -> set[str]:
        """Which of the vanished rows are NOT confirmed deleted (kept for another run).

        The transaction history confirms deletions in bulk. What it cannot confirm is
        asked for one by one: ``404`` is gone, anything else keeps the document.
        """
        known = self.state.get("txn_id") if self._fresh_state() else None
        confirmed: set[str] = set()
        history = None
        if isinstance(known, int):
            history = _history_deletes(self.client, known)
        if history is not None:
            confirmed, self._txn_after = history
        else:
            self._txn_after = _history_head(self.client)
        pending: set[str] = set()
        checks = 0
        for row_id in vanished:
            if row_id in confirmed:
                continue
            if checks >= _MAX_DELETE_CHECKS:
                pending.add(row_id)
                continue
            checks += 1
            kind = self._kind_of(row_id)
            handle = row_id.split(":", 2)[2]
            try:
                status, _, _ = self.client.get(
                    f"/{kind}/{handle}", {"keys": "handle"}, ok=(200, 404)
                )
            except GrampsAPIError as exc:
                logger.warning("Gramps Web: could not check a vanished %s (%s).", kind, exc)
                self.stats["failed"] += 1
                pending.add(row_id)
                continue
            if status != 404:
                pending.add(row_id)  # still there: the listing missed it, keep it
        return pending


def _iter_rows(
    client: GrampsClient,
    state: dict,
    stats: dict[str, int],
    *,
    include: tuple[str, ...] = DEFAULT_INCLUDE,
    tags: tuple[str, ...] | None = None,
    include_private: bool = False,
    full_sync_hours: float = 24.0,
    check_active: Callable[[], None] | None = None,
    now: Callable[[], float] = time.time,
) -> Iterator[dict]:
    """Yield the changed rows and tombstones of a tree. Pure of dlt, so tests use a dict."""
    client.start_run(check_active)
    run = _Run(
        client,
        state,
        stats,
        include=include,
        tags=tags,
        include_private=include_private,
        full_sync_hours=full_sync_hours,
        now=now,
    )
    yield from run.rows()
    stats["requests"] = client.requests


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def _clean_tuple(values: Iterable[str] | None, name: str) -> tuple[str, ...] | None:
    if values is None:
        return None
    if isinstance(values, str) or not all(isinstance(v, str) and v.strip() for v in values):
        raise ValueError(f"{name} must be a list of non-empty strings")
    cleaned = tuple(sorted({v.strip() for v in values}))
    if not cleaned:
        raise ValueError(f"{name} must not be empty; pass None for no filter")
    return cleaned


def gramps_source(
    *,
    base_url: str | None = None,
    username: str | None = None,
    password: str | None = None,
    resource_name: str | None = None,
    include: Iterable[str] = DEFAULT_INCLUDE,
    tags: Iterable[str] | None = None,
    include_private: bool = False,
    full_sync_hours: float = 24.0,
    check_active: Callable[[], None] | None = None,
    client: GrampsClient | None = None,
):
    """Return a ``dlt`` resource that yields a Gramps Web tree's documents for ``remember``.

    Hand it to ``cognee.remember(...)`` with ``primary_key="id"``,
    ``write_disposition="merge"`` and ``max_rows_per_table=0``.

    Args:
        base_url: Address of the Gramps Web server (``GRAMPS_URL``), without ``/api``.
        username: Gramps Web user (``GRAMPS_USERNAME``).
        password: That user's password (``GRAMPS_PASSWORD``).
        resource_name: Names the staging table and the sync state. Defaults to
            ``gramps_<12 hex>`` derived from the server address and the user. Two trees
            in one dataset need different names.
        include: Document kinds: ``"people"``, ``"families"`` and/or ``"places"``.
        tags: Only sync objects carrying at least one of these Gramps tag names.
        include_private: Also sync records, names and references marked private in
            Gramps. Off by default.
        full_sync_hours: A full pass runs at least this often, even when the quick
            check finds nothing changed.
        check_active: Optional host authorization checkpoint, called before each
            request and before each row is published.
        client: A pre-built :class:`GrampsClient` (reused tokens, or tests).
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 2:
        raise RuntimeError("Gramps sync requires cognee>=1.6.3 (per-row node sets).")
    include = tuple(dict.fromkeys(include))
    if not include or set(include) - set(KINDS):
        raise ValueError(f"include must be a non-empty subset of {KINDS}")
    tag_filter = _clean_tuple(tags, "tags")
    if not isinstance(full_sync_hours, (int, float)) or full_sync_hours <= 0:
        raise ValueError("full_sync_hours must be a positive number")
    if client is None:
        client = GrampsClient(
            base_url or os.environ.get("GRAMPS_URL", ""),
            username or os.environ.get("GRAMPS_USERNAME", ""),
            password or os.environ.get("GRAMPS_PASSWORD", ""),
        )
    if resource_name is None:
        identity = json.dumps([client.base_url, getattr(client, "_username", "")])
        resource_name = f"gramps_{hashlib.sha256(identity.encode()).hexdigest()[:12]}"
    elif not isinstance(resource_name, str) or not resource_name.strip():
        raise ValueError("resource_name must be a non-empty string")
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def gramps_tree():
        stats.clear()
        rows = _iter_rows(
            client,
            dlt.current.resource_state(),
            stats,
            include=include,
            tags=tag_filter,
            include_private=include_private,
            full_sync_hours=full_sync_hours,
            check_active=check_active,
        )
        yield from dlt_utils.guarded_rows(rows, check_active)

    resource = gramps_tree()
    setattr(resource, DOCUMENT_SOURCE_ATTR, GRAMPS_SOURCE_NAME)
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    resource.cognee_sync_stats = stats
    return resource
