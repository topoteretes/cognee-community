"""Pipedrive connector for cognee: a dlt resource that syncs your Pipedrive CRM into memory.

It ingests deals, persons, organizations, activities and notes as readable documents,
and is meant to be handed to ``cognee.remember``::

    await cognee.remember(
        pipedrive_source(api_token="<pipedrive api token>", resource_name="pipedrive_sales"),
        dataset_name="pipedrive",
        primary_key="id",
        write_disposition="merge",  # required, the add pipeline defaults to "replace"
    )
"""

import hashlib
import html
import json
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("pipedrive_connector")

# the token picks the company, so the shared api host works for every account
BASE_URL = "https://api.pipedrive.com"
# the record types this source syncs, in the order they are read: names of
# organizations and persons are known before the deals and notes that mention them
KINDS = ("organizations", "persons", "deals", "activities", "notes")
# the largest page size pipedrive allows on the v1 and v2 list endpoints
PAGE_SIZE = 500
# requests in one run; a bigger account finishes over several runs
DEFAULT_MAX_REQUESTS = 1000
# (connect, read) seconds; our own choice, pipedrive documents no expected latency
_TIMEOUT = (10, 30)
# attempts per request on 5xx, 429 or network errors
_RETRIES = 3
# pipedrive's burst window is 2 seconds; a longer Retry-After means the daily budget
# is spent, and sleeping on it would hold cognee's staging lock for hours
_MAX_RETRY_WAIT = 10.0
# resource_name becomes a dlt table and pipeline name, which the filesystem limits
_MAX_NAME_LENGTH = 64
# the singular of each kind, used in row ids, app urls and the GET re-check
_SINGULAR = {
    "organizations": "organization",
    "persons": "person",
    "deals": "deal",
    "activities": "activity",
    "notes": "note",
}
# v2 list endpoints use cursor paging; notes have no v2 endpoint yet
_LISTINGS = {
    "organizations": ("/api/v2/organizations", "cursor"),
    "persons": ("/api/v2/persons", "cursor"),
    "deals": ("/api/v2/deals", "cursor"),
    # the regular deal listing leaves archived deals out
    "archived_deals": ("/api/v2/deals/archived", "cursor"),
    "activities": ("/api/v2/activities", "cursor"),
    "notes": ("/v1/notes", "offset"),
}
_SHOW = {
    "organization": "/api/v2/organizations/{id}",
    "person": "/api/v2/persons/{id}",
    "deal": "/api/v2/deals/{id}",
    "activity": "/api/v2/activities/{id}",
    "note": "/v1/notes/{id}",
}


class PipedriveSourceError(RuntimeError):
    """Base class of the errors this source raises."""


class PipedriveAPIError(PipedriveSourceError):
    """Pipedrive answered with an error, or could not be reached."""


class PipedriveAuthError(PipedriveSourceError):
    """Pipedrive rejected the API token (HTTP 401)."""


class PipedriveAccessError(PipedriveSourceError):
    """The token's user cannot call an endpoint (HTTP 403)."""


class PipedriveRateLimitedError(PipedriveSourceError):
    """Pipedrive still answered HTTP 429 after the retries."""


class PipedriveNotFoundError(PipedriveAPIError):
    """The record was deleted or never existed."""


def _endpoint(path: str) -> str:
    """The path with a record id replaced, so error messages never carry one."""
    return re.sub(r"/\d+$", "/{id}", path)


def _retry_after(headers: Any) -> float | None:
    try:
        return float(headers.get("retry-after"))
    except (TypeError, ValueError):
        return None


class PipedriveClient:
    """Minimal synchronous REST client for Pipedrive.

    Tests can pass any object with the same ``request`` method instead.
    """

    def __init__(self, api_token: str, *, sleep: Callable[[float], None] = time.sleep):
        token = (api_token or "").strip()
        # reject early and without echoing the token: requests would put it in its exception
        if not token or not token.isascii() or not token.isprintable() or " " in token:
            raise ValueError("The Pipedrive API token is empty or has an invalid format")
        self._api_token = token
        self._sleep = sleep

    def __repr__(self) -> str:
        return "PipedriveClient(api_token=<redacted>)"

    def request(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        check_active: Callable[[], None] | None = None,
    ) -> dict:
        headers = {"x-api-token": self._api_token, "Accept": "application/json"}
        for attempt in range(_RETRIES):
            last = attempt == _RETRIES - 1
            if check_active is not None:
                check_active()
            try:
                response = requests.get(
                    BASE_URL + path, params=params, headers=headers, timeout=_TIMEOUT
                )
            except requests.RequestException as exc:
                if last:
                    raise PipedriveAPIError(
                        f"Pipedrive request failed: {type(exc).__name__}"
                    ) from None
                self._sleep(2**attempt)
                continue
            status = response.status_code
            if status == 429 and not last:
                wait = _retry_after(response.headers)
                if wait is not None and wait > _MAX_RETRY_WAIT:
                    raise PipedriveRateLimitedError("Pipedrive rate limit exceeded (HTTP 429)")
                self._sleep(wait if wait is not None else 2**attempt)
                continue
            if status >= 500 and not last:
                self._sleep(2**attempt)
                continue
            # the last attempt always ends here, so the loop never falls through
            return self._parse(response, path)

    def _parse(self, response: Any, path: str) -> dict:
        status = response.status_code
        if status == 401:
            raise PipedriveAuthError("Pipedrive rejected the API token (HTTP 401)")
        if status == 403:
            raise PipedriveAccessError(
                f"The Pipedrive user cannot call {_endpoint(path)} (HTTP 403)"
            )
        if status == 429:
            raise PipedriveRateLimitedError("Pipedrive rate limit exceeded (HTTP 429)")
        if status in (404, 410):
            raise PipedriveNotFoundError(f"Pipedrive record not found (HTTP {status})")
        if status != 200:
            raise PipedriveAPIError(f"Pipedrive request failed: HTTP {status}")
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise PipedriveAPIError(f"Pipedrive sent an unreadable answer for {_endpoint(path)}")
        return payload


# ---------------------------------------------------------------------------
# Rendering: deterministic, readable fields only, nothing volatile.
# ---------------------------------------------------------------------------
@dataclass
class Lookups:
    """Id-to-name maps that turn Pipedrive ids into readable text."""

    users: dict[Any, str] = field(default_factory=dict)
    pipelines: dict[Any, str] = field(default_factory=dict)
    stages: dict[Any, str] = field(default_factory=dict)
    # per kind: custom field key -> (name, field type, option id -> label)
    fields: dict[str, dict[str, tuple[str, str, dict[str, str]]]] = field(default_factory=dict)
    # per kind: label option id -> label
    labels: dict[str, dict[str, str]] = field(default_factory=dict)
    # "<kind>:<id>" -> name or title, kept across runs in the resource state
    names: dict[str, str] = field(default_factory=dict)
    # the company's subdomain, for links back into the app
    domain: str = ""


class _TextExtractor(HTMLParser):
    _BREAKS = frozenset(
        {"br", "p", "div", "li", "ul", "ol", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}
    )

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "li":
            self.parts.append("\n- ")
        elif tag in self._BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._BREAKS and tag != "li":
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(value: Any) -> str:
    """Plain text of a note or activity body, which pipedrive stores as HTML."""
    if not isinstance(value, str) or not value.strip():
        return ""
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    text = html.unescape("".join(parser.parts)).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _text(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(sorted(_text(item) for item in value if _text(item)))
    if isinstance(value, bool):
        return "yes" if value else "no"
    return "" if value is None else str(value).strip()


def _lines(fields: list[tuple[str, str]]) -> list[str]:
    return [f"{label}: {value}" for label, value in fields if value]


def _name(lookups: Lookups, kind: str, record_id: Any) -> str:
    return lookups.names.get(f"{kind}:{record_id}", "") if record_id else ""


def _money(value: Any, currency: Any) -> str:
    if value in (None, ""):
        return ""
    return f"{_text(value)} {_text(currency)}".strip()


def _field_value(value: Any, field_type: str, options: dict[str, str], lookups: Lookups) -> str:
    if value is None or value == "" or value == []:
        return ""
    if options:
        ids = value if isinstance(value, list) else str(value).split(",")
        return ", ".join(sorted(options.get(str(i).strip(), _text(i)) for i in ids))
    if field_type == "user":
        return lookups.users.get(value, "")
    if field_type == "org":
        return _name(lookups, "organization", value)
    if field_type == "people":
        return _name(lookups, "person", value)
    if isinstance(value, dict):
        if "value" in value:
            return _money(value.get("value"), value.get("currency"))
        return ", ".join(f"{k}: {_text(v)}" for k, v in sorted(value.items()) if _text(v))
    return _text(value)


def _custom_fields(record: dict, kind: str, lookups: Lookups) -> list[tuple[str, str]]:
    values = record.get("custom_fields")
    if not isinstance(values, dict):
        return []
    defs = lookups.fields.get(kind, {})
    pairs = []
    for key, value in values.items():
        if key not in defs:
            continue
        name, field_type, options = defs[key]
        text = _field_value(value, field_type, options, lookups)
        if name and text:
            pairs.append((name, text))
    return sorted(pairs)


def _labels(record: dict, kind: str, lookups: Lookups) -> str:
    ids = record.get("label_ids") or []
    options = lookups.labels.get(kind, {})
    return ", ".join(sorted({options[str(i)] for i in ids if str(i) in options}))


def _url(lookups: Lookups, kind: str, record_id: Any) -> str | None:
    if not lookups.domain or not record_id:
        return None
    return f"https://{lookups.domain}.pipedrive.com/{kind}/{record_id}"


def _row(row_id: str, title: str, fields: list[tuple[str, str]], body: str, url) -> dict:
    parts = _lines(fields)
    if body:
        parts.extend(["", body])
    return {
        "id": row_id,
        "title": title,
        "content": "\n".join(parts).strip() or title,
        "url": url,
        "_deleted": False,
    }


def render_organization(org: dict, lookups: Lookups) -> dict:
    title = _text(org.get("name")) or "Unnamed organization"
    address = org.get("address")
    fields = [
        ("Owner", lookups.users.get(org.get("owner_id"), "")),
        ("Address", _text(address.get("value")) if isinstance(address, dict) else _text(address)),
        ("Labels", _labels(org, "organization", lookups)),
        *_custom_fields(org, "organization", lookups),
    ]
    return _row(
        f"organization:{org['id']}", title, fields, "", _url(lookups, "organization", org["id"])
    )


def render_person(person: dict, lookups: Lookups) -> dict:
    title = _text(person.get("name")) or "Unnamed person"
    fields = [
        ("Job title", _text(person.get("job_title"))),
        ("Organization", _name(lookups, "organization", person.get("org_id"))),
        ("Owner", lookups.users.get(person.get("owner_id"), "")),
        ("Labels", _labels(person, "person", lookups)),
        *_custom_fields(person, "person", lookups),
    ]
    return _row(f"person:{person['id']}", title, fields, "", _url(lookups, "person", person["id"]))


def render_deal(deal: dict, lookups: Lookups) -> dict:
    title = _text(deal.get("title")) or "Untitled deal"
    status = _text(deal.get("status"))
    if deal.get("is_archived"):
        status = f"{status} (archived)".strip()
    fields = [
        ("Status", status),
        ("Pipeline", lookups.pipelines.get(deal.get("pipeline_id"), "")),
        ("Stage", lookups.stages.get(deal.get("stage_id"), "")),
        ("Value", _money(deal.get("value"), deal.get("currency"))),
        ("Organization", _name(lookups, "organization", deal.get("org_id"))),
        ("Person", _name(lookups, "person", deal.get("person_id"))),
        ("Owner", lookups.users.get(deal.get("owner_id"), "")),
        ("Expected close date", _text(deal.get("expected_close_date"))),
        ("Won on", _text(deal.get("won_time"))[:10]),
        ("Lost on", _text(deal.get("lost_time"))[:10]),
        ("Lost reason", _text(deal.get("lost_reason"))),
        ("Labels", _labels(deal, "deal", lookups)),
        *_custom_fields(deal, "deal", lookups),
    ]
    return _row(f"deal:{deal['id']}", title, fields, "", _url(lookups, "deal", deal["id"]))


def _parents(record: dict, lookups: Lookups) -> list[tuple[str, str]]:
    """The deal, person and organization a note or activity belongs to."""
    parents = []
    for kind, id_key, name_key in (
        ("deal", "deal_id", "title"),
        ("person", "person_id", "name"),
        ("organization", "org_id", "name"),
    ):
        # v1 notes embed the parent's name; v2 activities carry only its id
        embedded = record.get(kind)
        fallback = _text(embedded.get(name_key)) if isinstance(embedded, dict) else ""
        parents.append((kind.capitalize(), _name(lookups, kind, record.get(id_key)) or fallback))
    return parents


def _parent_url(record: dict, lookups: Lookups) -> str | None:
    for kind, key in (("deal", "deal_id"), ("person", "person_id"), ("organization", "org_id")):
        if record.get(key):
            return _url(lookups, kind, record[key])
    return None


def render_activity(activity: dict, lookups: Lookups) -> dict:
    title = _text(activity.get("subject")) or "Untitled activity"
    due = f"{_text(activity.get('due_date'))} {_text(activity.get('due_time'))}".strip()
    location = activity.get("location")
    fields = [
        ("Type", _text(activity.get("type"))),
        ("Due", due),
        ("Done", _text(bool(activity.get("done")))),
        *_parents(activity, lookups),
        ("Owner", lookups.users.get(activity.get("owner_id"), "")),
        (
            "Location",
            _text(location.get("value")) if isinstance(location, dict) else _text(location),
        ),
        ("Public description", html_to_text(activity.get("public_description"))),
    ]
    return _row(
        f"activity:{activity['id']}",
        title,
        fields,
        html_to_text(activity.get("note")),
        _parent_url(activity, lookups),
    )


def render_note(note: dict, lookups: Lookups) -> dict:
    parents = _parents(note, lookups)
    about = next((f"{kind.lower()} {name}" for kind, name in parents if name), "")
    title = f"Note on {about}" if about else "Note"
    author = lookups.users.get(note.get("user_id"), "")
    if not author and isinstance(note.get("user"), dict):
        author = _text(note["user"].get("name"))
    fields = [*parents, ("Author", author), ("Added", _text(note.get("add_time"))[:10])]
    return _row(
        f"note:{note['id']}",
        title,
        fields,
        html_to_text(note.get("content")),
        _parent_url(note, lookups),
    )


_RENDER = {
    "organizations": render_organization,
    "persons": render_person,
    "deals": render_deal,
    "archived_deals": render_deal,
    "activities": render_activity,
    "notes": render_note,
}


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
class _CutShortError(Exception):
    """Internal: stop the run here, keeping the state reached so far."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _walk_names(include: tuple[str, ...], include_archived: bool) -> list[str]:
    """The listings one cycle reads, in order."""
    walks = []
    for kind in KINDS:
        if kind in include:
            walks.append(kind)
            if kind == "deals" and include_archived:
                walks.append("archived_deals")
    return walks


def _records(payload: dict, path: str) -> list:
    data = payload.get("data")
    # v1 answers an empty listing with null instead of []
    if data is None:
        return []
    if not isinstance(data, list):
        raise PipedriveAPIError(f"Pipedrive sent no list for {_endpoint(path)}")
    return data


def _next_cursor(payload: dict, path: str) -> str | None:
    extra = payload.get("additional_data")
    if not isinstance(extra, dict) or "next_cursor" not in extra:
        # without it a listing can't be trusted as complete
        raise PipedriveAPIError(f"Pipedrive answered {_endpoint(path)} without a cursor")
    return extra["next_cursor"] or None


def _next_start(payload: dict, path: str, start: int) -> int | None:
    extra = payload.get("additional_data")
    pagination = extra.get("pagination") if isinstance(extra, dict) else None
    if not isinstance(pagination, dict) or "more_items_in_collection" not in pagination:
        raise PipedriveAPIError(f"Pipedrive answered {_endpoint(path)} without pagination")
    if not pagination["more_items_in_collection"]:
        return None
    next_start = pagination.get("next_start")
    if not isinstance(next_start, int) or next_start <= start:
        raise PipedriveAPIError(f"Pipedrive sent a bad next page for {_endpoint(path)}")
    return next_start


class _Walker:
    """One run over the selected kinds, writing progress into ``state``.

    Pipedrive has no delete feed for persons, organizations, activities or notes,
    so every cycle lists each kind in full and emits a row only when its
    fingerprint changed. Ids that stopped appearing are re-checked one by one
    before they are forgotten.
    """

    def __init__(
        self,
        client: Any,
        state: dict,
        stats: dict[str, int],
        *,
        include: tuple[str, ...],
        include_archived: bool,
        max_requests: int,
        check_active: Callable[[], None] | None,
    ):
        self.client = client
        self.state = state
        self.stats = stats
        for key in ("scanned", "skipped", "deleted", "failed"):
            stats.setdefault(key, 0)
        self.include = include
        self.include_archived = include_archived
        self.max_requests = max_requests
        self.check_active = check_active
        self.requests = 0
        self.emitted = 0
        self.walks = _walk_names(include, include_archived)
        self.fingerprints: dict[str, str] = state.setdefault("fingerprints", {})
        self.seen: list[str] = state.setdefault("seen", [])
        self.lookups = Lookups(names=state.setdefault("names", {}))
        # a cycle started under another selection must not decide deletions
        cycle = _hash(self.walks)
        if state.get("cycle") != cycle:
            state["cycle"] = cycle
            state["walks"] = {}
            self.seen.clear()

    # -- requests ----------------------------------------------------------
    def _call(self, path: str, params: dict[str, Any] | None = None) -> dict:
        if self.check_active is not None:
            self.check_active()
        if self.requests >= self.max_requests:
            raise _CutShortError("budget")
        self.requests += 1
        try:
            return self.client.request(path, params=params, check_active=self.check_active)
        except PipedriveRateLimitedError:
            raise _CutShortError("rate_limit") from None
        except PipedriveAuthError:
            if self.emitted:
                raise _CutShortError("auth") from None
            raise

    def _all(self, path: str, mode: str) -> Iterator[dict]:
        """Every record of a small lookup listing, read in one go."""
        cursor: str | None = None
        start = 0
        seen_cursors: set[str] = set()
        while True:
            if mode == "cursor":
                params: dict[str, Any] = {"limit": PAGE_SIZE}
                if cursor:
                    params["cursor"] = cursor
            else:
                params = {"limit": PAGE_SIZE, "start": start}
            payload = self._call(path, params)
            yield from (r for r in _records(payload, path) if isinstance(r, dict))
            if mode == "cursor":
                cursor = _next_cursor(payload, path)
                if cursor is None:
                    return
                if cursor in seen_cursors:
                    raise PipedriveAPIError(f"Pipedrive repeated a cursor for {_endpoint(path)}")
                seen_cursors.add(cursor)
            else:
                next_start = _next_start(payload, path, start)
                if next_start is None:
                    return
                start = next_start

    # -- lookups -----------------------------------------------------------
    def _load_lookups(self) -> None:
        me = self._call("/v1/users/me").get("data")
        if not isinstance(me, dict):
            raise PipedriveAPIError("Pipedrive sent no user for /v1/users/me")
        self._check_company(me.get("company_id"))
        self.lookups.domain = _text(me.get("company_domain"))
        users = _records(self._call("/v1/users"), "/v1/users")
        self.lookups.users = {u["id"]: _text(u.get("name")) for u in users if "id" in u}
        if "deals" in self.include:
            pipelines = self._all("/api/v2/pipelines", "cursor")
            self.lookups.pipelines = {p["id"]: _text(p.get("name")) for p in pipelines}
            stages = self._all("/api/v2/stages", "cursor")
            self.lookups.stages = {s["id"]: _text(s.get("name")) for s in stages}
        for kind, path in (
            ("organization", "/v1/organizationFields"),
            ("person", "/v1/personFields"),
            ("deal", "/v1/dealFields"),
        ):
            if f"{kind}s" in self.include:
                self._load_fields(kind, self._all(path, "offset"))

    def _load_fields(self, kind: str, definitions: Iterator[dict]) -> None:
        fields: dict[str, tuple[str, str, dict[str, str]]] = {}
        labels: dict[str, str] = {}
        for definition in definitions:
            key = definition.get("key")
            options = {
                str(o["id"]): _text(o.get("label"))
                for o in definition.get("options") or []
                if isinstance(o, dict) and "id" in o
            }
            if key in ("label", "label_ids"):
                labels = options
            elif isinstance(key, str):
                fields[key] = (
                    _text(definition.get("name")),
                    _text(definition.get("field_type")),
                    options,
                )
        self.lookups.fields[kind] = fields
        self.lookups.labels[kind] = labels

    def _check_company(self, company_id: Any) -> None:
        """Refuse state written for another company, which would forget that company's rows."""
        if company_id is None:
            raise PipedriveAPIError("Pipedrive sent no company for /v1/users/me")
        stored = self.state.setdefault("company_id", company_id)
        if stored != company_id:
            raise PipedriveSourceError(
                "This resource_name already holds another Pipedrive company's sync; "
                "give each Pipedrive company its own resource_name"
            )

    # -- listing -----------------------------------------------------------
    def _walk(self, walk: str) -> Iterator[dict]:
        """Yield every record of a listing, resuming at the page a cut run stopped on."""
        path, mode = _LISTINGS[walk]
        progress = self.state.setdefault("walks", {}).setdefault(walk, {})
        if progress.get("done"):
            return
        while True:
            if mode == "cursor":
                params: dict[str, Any] = {"limit": PAGE_SIZE}
                if progress.get("cursor"):
                    params["cursor"] = progress["cursor"]
            else:
                params = {"limit": PAGE_SIZE, "start": progress.get("start", 0), "sort": "id ASC"}
            payload = self._call(path, params)
            records = _records(payload, path)
            yield from records
            if mode == "cursor":
                cursor = _next_cursor(payload, path)
                if cursor is None:
                    break
                if cursor in progress.setdefault("cursors", []):
                    raise PipedriveAPIError(f"Pipedrive repeated a cursor for {_endpoint(path)}")
                progress["cursors"].append(cursor)
                progress["cursor"] = cursor
            else:
                next_start = _next_start(payload, path, progress.get("start", 0))
                if next_start is None:
                    break
                progress["start"] = next_start
        progress["done"] = True

    def _changed(self, row_id: str, fingerprint: str) -> bool:
        self.seen.append(row_id)
        self.stats["scanned"] += 1
        if self.fingerprints.get(row_id) == fingerprint:
            self.stats["skipped"] += 1
            return False
        return True

    def _emit(self, row: dict, fingerprint: str) -> dict:
        self.fingerprints[row["id"]] = fingerprint
        self.emitted += 1
        return row

    def _rows_of(self, walk: str) -> Iterator[dict]:
        render = _RENDER[walk]
        for record in self._walk(walk):
            if not isinstance(record, dict) or record.get("id") is None:
                self._failed("listing", PipedriveAPIError("a listed record has no id"))
                continue
            # a deleted record left in a listing is not seen, so the re-check forgets it
            if record.get("is_deleted") or record.get("active_flag") is False:
                continue
            row = render(record, self.lookups)
            if walk in ("organizations", "persons", "deals", "archived_deals"):
                raw = record.get("title") if walk.endswith("deals") else record.get("name")
                self.lookups.names[row["id"]] = _text(raw)
            fingerprint = _hash(row)
            if self._changed(row["id"], fingerprint):
                yield self._emit(row, fingerprint)

    def _failed(self, step: str, exc: Exception) -> None:
        self.stats["failed"] += 1
        # the message names the status and endpoint, never the record id
        logger.warning("Pipedrive %s failed for one record, retried next run: %s", step, exc)

    # -- forget-on-delete --------------------------------------------------
    def _close_cycle(self) -> None:
        """Queue ids not seen in a complete cycle, then start a fresh one."""
        missing = set(self.fingerprints) - set(self.seen)
        self.state["pending_deletes"] = sorted(missing)
        self.state["walks"] = {}
        self.seen.clear()

    def _forget(self) -> Iterator[dict]:
        pending = self.state.setdefault("pending_deletes", [])
        while pending:
            row_id = pending[0]
            try:
                gone = self._gone(row_id)
            except (PipedriveAPIError, PipedriveAccessError) as exc:
                # keep the row; its fingerprint queues it again when this cycle closes
                self._failed("deletion check", exc)
                gone = False
            if gone:
                self.fingerprints.pop(row_id, None)
                self.lookups.names.pop(row_id, None)
                self.stats["deleted"] += 1
                yield {"id": row_id, "_deleted": True}
            pending.pop(0)

    def _gone(self, row_id: str) -> bool:
        singular, record_id = row_id.split(":", 1)
        kind = next(k for k, s in _SINGULAR.items() if s == singular)
        if kind not in self.include:
            return True  # deselected kinds leave memory
        try:
            payload = self._call(_SHOW[singular].format(id=record_id))
        except PipedriveNotFoundError:
            return True
        record = payload.get("data")
        if not isinstance(record, dict):
            raise PipedriveAPIError(f"Pipedrive sent no {singular} for {_SHOW[singular]}")
        if record.get("is_deleted") or record.get("active_flag") is False:
            return True
        if singular == "deal":
            if record.get("status") == "deleted":
                return True
            if record.get("is_archived") and not self.include_archived:
                return True
        # still there: a listing that shifted under us, it is seen again next cycle
        return False

    # -- the run -----------------------------------------------------------
    def rows(self) -> Iterator[dict]:
        self._load_lookups()
        # finish deletions a cut run left behind before starting a new listing
        yield from self._forget()
        for walk in self.walks:
            yield from self._rows_of(walk)
        self._close_cycle()
        yield from self._forget()


def _iter_rows(
    client: Any,
    state: dict,
    stats: dict[str, int],
    *,
    include: tuple[str, ...] = KINDS,
    include_archived: bool = True,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict]:
    """Yield the changed rows of a company. Pure of dlt, so tests drive it with a dict."""
    walker = _Walker(
        client,
        state,
        stats,
        include=include,
        include_archived=include_archived,
        max_requests=max_requests,
        check_active=check_active,
    )
    try:
        yield from walker.rows()
    except _CutShortError as cut:
        stats["failed"] = max(1, stats.get("failed", 0))
        stats[f"failed_{cut.reason}"] = 1
        logger.warning("Pipedrive sync stopped early (%s); it resumes on the next run.", cut.reason)


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def pipedrive_source(
    *,
    resource_name: str,
    api_token: str | None = None,
    service: Any = None,
    check_active: Callable[[], None] | None = None,
    include: tuple[str, ...] = KINDS,
    include_archived_deals: bool = True,
    max_requests: int = DEFAULT_MAX_REQUESTS,
):
    """Return a ``dlt`` resource that yields a Pipedrive company's documents for ``remember``.

    Args:
        resource_name: Stable name of this connection, one per Pipedrive company. It
            scopes the sync state and the cleanup of deleted records, so keep it fixed
            across runs. A run refuses state written for another company.
        api_token: Pipedrive personal API token. Used to build the client when
            ``service`` is omitted.
        service: Pre-built client, mainly an injection point for tests.
        check_active: Optional host authorization checkpoint, called before each
            request.
        include: Which of ``"organizations"``, ``"persons"``, ``"deals"``,
            ``"activities"`` and ``"notes"`` to sync.
        include_archived_deals: Also sync archived deals. When off, deals that get
            archived are forgotten.
        max_requests: Request budget of one run. A bigger company finishes over
            several runs.

    Returns:
        A ``dlt`` resource configured with ``primary_key="id"``,
        ``write_disposition="merge"`` and a ``_deleted`` hard-delete column.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError("Pipedrive sync requires cognee>=1.6.3 (table-scoped document cleanup).")
    if (
        not isinstance(resource_name, str)
        or not resource_name.strip()
        or len(resource_name) > _MAX_NAME_LENGTH
    ):
        raise ValueError(
            f"pipedrive_source needs a resource_name of 1 to {_MAX_NAME_LENGTH} characters, "
            "one per Pipedrive company"
        )
    if service is None and not api_token:
        raise ValueError("pipedrive_source needs a service or an api_token")
    include = tuple(include)
    if not include or set(include) - set(KINDS):
        raise ValueError(f"include must be a non-empty subset of {KINDS}")
    if not isinstance(max_requests, int) or max_requests < 1:
        raise ValueError("max_requests must be a positive integer")

    # built here so a malformed token fails at construction and the closure holds no raw token
    client = service if service is not None else PipedriveClient(api_token or "")
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def pipedrive_company():
        stats.clear()
        stats.update(scanned=0, skipped=0, deleted=0, failed=0)
        rows = _iter_rows(
            client,
            dlt.current.resource_state(),
            stats,
            include=include,
            include_archived=include_archived_deals,
            max_requests=max_requests,
            check_active=check_active,
        )
        yield from dlt_utils.guarded_rows(rows, check_active)

    resource = pipedrive_company()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "pipedrive")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    resource.cognee_sync_stats = stats
    return resource
