"""Copper connector for cognee: a dlt resource that syncs your Copper CRM into memory.

It ingests companies, people, opportunities and their activity log (notes, calls,
meetings) as readable documents, and is meant to be handed to ``cognee.remember``::

    await cognee.remember(
        copper_source(
            api_key="<copper api key>",
            user_email="<email of the user who made the key>",
            resource_name="copper_sales",
        ),
        dataset_name="copper",
        primary_key="id",
        write_disposition="merge",  # required, the add pipeline defaults to "replace"
    )
"""

import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("copper_connector")

BASE_URL = "https://api.copper.com/developer_api/v1"
APP_URL = "https://app.copper.com/companies"
# the record types this source syncs, in the order they are read: names of companies
# and people are known before the opportunities and activities that mention them
KINDS = ("companies", "people", "opportunities", "activities")
# copper's largest page_size on the search endpoints
PAGE_SIZE = 200
# copper search returns at most the first 100,000 records of a query
SEARCH_CAP = 100_000
# requests in one run; a bigger account finishes over several runs
DEFAULT_MAX_REQUESTS = 1000
# (connect, read) seconds; our own choice, copper documents no expected latency
_TIMEOUT = (10, 30)
# attempts per request on 5xx, 429 or network errors
_RETRIES = 3
# copper limits requests per minute, so a 429 clears within a minute; a longer
# Retry-After is not slept on, since it would hold cognee's staging lock
_MAX_RETRY_WAIT = 60.0
# total seconds one run may spend waiting on retries before it stops and resumes later
_MAX_RUN_WAIT = 120.0
# resource_name becomes a dlt table and pipeline name, which the filesystem limits
_MAX_NAME_LENGTH = 64
# copper sends the key and the email in these headers, plus a fixed application name
_KEY_HEADER = "X-PW-AccessToken"
_EMAIL_HEADER = "X-PW-UserEmail"
# the singular of each kind, used in row ids and the GET re-check
_SINGULAR = {
    "companies": "company",
    "people": "person",
    "opportunities": "opportunity",
    "activities": "activity",
}
# the part of an app link after "#/" for each kind
_APP_PATHS = {"company": "organization", "person": "contact", "opportunity": "deal"}


class CopperSourceError(RuntimeError):
    """Base class of the errors this source raises."""


class CopperAPIError(CopperSourceError):
    """Copper answered with an error, or could not be reached."""


class CopperAuthError(CopperSourceError):
    """Copper rejected the API key and email (HTTP 401)."""


class CopperAccessError(CopperSourceError):
    """The key's user cannot call an endpoint (HTTP 403)."""


class CopperRateLimitedError(CopperSourceError):
    """Copper still answered HTTP 429 after the retries."""


class CopperNotFoundError(CopperAPIError):
    """The record was deleted or never existed."""


def _endpoint(path: str) -> str:
    """The path with a record id replaced, so error messages never carry one."""
    return re.sub(r"/\d+$", "/{id}", path)


def _retry_after(headers: Any) -> float | None:
    try:
        wait = float(headers.get("retry-after"))
    except (TypeError, ValueError):
        return None
    # time.sleep refuses nan and negative values
    return wait if math.isfinite(wait) and wait >= 0 else None


def _header_value(value: str | None, what: str) -> str:
    text = (value or "").strip()
    # reject early and without echoing the value: requests would put it in its exception
    if not text or not text.isascii() or not text.isprintable() or " " in text:
        raise ValueError(f"The Copper {what} is empty or has an invalid format")
    return text


class CopperClient:
    """Minimal synchronous REST client for Copper.

    Copper needs the API key and the email of the user who created it on every
    request. Tests can pass any object with the same ``request`` method instead.
    """

    def __init__(
        self, api_key: str, user_email: str, *, sleep: Callable[[float], None] = time.sleep
    ):
        self._api_key = _header_value(api_key, "API key")
        email = _header_value(user_email, "user email")
        if "@" not in email:
            raise ValueError(
                "The Copper user email must be the email of the user who created the API key"
            )
        self._user_email = email
        self._sleep = sleep
        self._waited = 0.0

    def start_run(self) -> None:
        """Reset the retry wait budget; called at the start of every sync run."""
        self._waited = 0.0

    def _wait(self, seconds: float) -> None:
        if self._waited + seconds > _MAX_RUN_WAIT:
            raise CopperRateLimitedError("Copper kept this run waiting too long on retries")
        self._waited += seconds
        self._sleep(seconds)

    def __repr__(self) -> str:
        return "CopperClient(api_key=<redacted>)"

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        check_active: Callable[[], None] | None = None,
    ) -> Any:
        headers = {
            _KEY_HEADER: self._api_key,
            _EMAIL_HEADER: self._user_email,
            "X-PW-Application": "developer_api",
            "Content-Type": "application/json",
        }
        for attempt in range(_RETRIES):
            last = attempt == _RETRIES - 1
            if check_active is not None:
                check_active()
            try:
                # no redirects: requests would forward the key headers to any host
                response = requests.request(
                    method,
                    BASE_URL + path,
                    json=body,
                    headers=headers,
                    timeout=_TIMEOUT,
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                if last:
                    raise CopperAPIError(f"Copper request failed: {type(exc).__name__}") from None
                self._wait(2**attempt)
                continue
            status = response.status_code
            if status == 429 and not last:
                wait = _retry_after(response.headers)
                if wait is not None and wait > _MAX_RETRY_WAIT:
                    raise CopperRateLimitedError("Copper rate limit exceeded (HTTP 429)")
                self._wait(wait if wait is not None else 2 ** (attempt + 2))
                continue
            if status >= 500 and not last:
                self._wait(2**attempt)
                continue
            # the last attempt always ends here, so the loop never falls through
            return self._parse(response, path)

    def _parse(self, response: Any, path: str) -> Any:
        status = response.status_code
        if status == 401:
            raise CopperAuthError(
                "Copper rejected the API key and user email (HTTP 401); the email must "
                "belong to the user who created the key"
            )
        if status == 403:
            raise CopperAccessError(f"The Copper user cannot call {_endpoint(path)} (HTTP 403)")
        if status == 429:
            raise CopperRateLimitedError("Copper rate limit exceeded (HTTP 429)")
        if status in (404, 410):
            raise CopperNotFoundError(f"Copper record not found (HTTP {status})")
        if status != 200:
            raise CopperAPIError(f"Copper request failed: HTTP {status}")
        try:
            payload = response.json()
        except ValueError:
            payload = None
        # searches and lookups answer with a list, single records with an object
        if not isinstance(payload, (list, dict)):
            raise CopperAPIError(f"Copper sent an unreadable answer for {_endpoint(path)}")
        return payload


# ---------------------------------------------------------------------------
# Rendering: deterministic, readable fields only, nothing volatile.
# ---------------------------------------------------------------------------
@dataclass
class Lookups:
    """Id-to-name maps that turn Copper ids into readable text."""

    users: dict[Any, str] = field(default_factory=dict)
    pipelines: dict[Any, str] = field(default_factory=dict)
    stages: dict[Any, str] = field(default_factory=dict)
    contact_types: dict[Any, str] = field(default_factory=dict)
    customer_sources: dict[Any, str] = field(default_factory=dict)
    loss_reasons: dict[Any, str] = field(default_factory=dict)
    activity_types: dict[Any, str] = field(default_factory=dict)
    # custom field definition id -> (name, data type, option id -> name)
    fields: dict[Any, tuple[str, str, dict[Any, str]]] = field(default_factory=dict)
    # "<kind>:<id>" -> name, kept across runs in the resource state
    names: dict[str, str] = field(default_factory=dict)
    account_id: Any = None


def _text(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(sorted(_text(item) for item in value if _text(item)))
    if isinstance(value, bool):
        return "yes" if value else "no"
    return "" if value is None else str(value).strip()


def _date(timestamp: Any) -> str:
    """A unix timestamp as a calendar date (UTC)."""
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        return ""
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _lines(fields: list[tuple[str, str]]) -> list[str]:
    return [f"{label}: {value}" for label, value in fields if value]


def _is_id(value: Any) -> bool:
    return isinstance(value, (int, str)) and not isinstance(value, bool)


def _lookup(names: dict[Any, str], record_id: Any) -> str:
    """The name of an id, or "" for an id of an unexpected shape."""
    return names.get(record_id, "") if _is_id(record_id) else ""


def _name(lookups: Lookups, kind: str, record_id: Any) -> str:
    if not record_id or not _is_id(record_id):
        return ""
    return lookups.names.get(f"{kind}:{record_id}", "")


def _custom_fields(record: dict, lookups: Lookups) -> list[tuple[str, str]]:
    values = record.get("custom_fields")
    if not isinstance(values, list):
        return []
    pairs = []
    for item in values:
        if (
            not isinstance(item, dict)
            or item.get("custom_field_definition_id") not in lookups.fields
        ):
            continue
        name, data_type, options = lookups.fields[item["custom_field_definition_id"]]
        value = item.get("value")
        if value is None or value == "" or value == []:
            continue
        if options:
            ids = value if isinstance(value, list) else [value]
            text = ", ".join(sorted(_lookup(options, i) or _text(i) for i in ids))
        elif data_type == "Date":
            text = _date(value)
        else:
            text = _text(value)
        if name and text:
            pairs.append((name, text))
    return sorted(pairs)


def _url(lookups: Lookups, kind: str, record_id: Any) -> str | None:
    if lookups.account_id is None or not record_id or kind not in _APP_PATHS:
        return None
    return f"{APP_URL}/{lookups.account_id}/app#/{_APP_PATHS[kind]}/{record_id}"


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


def render_company(company: dict, lookups: Lookups) -> dict:
    title = _text(company.get("name")) or "Unnamed company"
    fields = [
        ("Email domain", _text(company.get("email_domain"))),
        ("Type", _lookup(lookups.contact_types, company.get("contact_type_id"))),
        ("Owner", _lookup(lookups.users, company.get("assignee_id"))),
        ("Tags", _text(company.get("tags"))),
        *_custom_fields(company, lookups),
    ]
    return _row(
        f"company:{company['id']}",
        title,
        fields,
        _text(company.get("details")),
        _url(lookups, "company", company["id"]),
    )


def render_person(person: dict, lookups: Lookups) -> dict:
    title = _text(person.get("name")) or "Unnamed person"
    company = _name(lookups, "company", person.get("company_id")) or _text(
        person.get("company_name")
    )
    fields = [
        ("Job title", _text(person.get("title"))),
        ("Company", company),
        ("Type", _lookup(lookups.contact_types, person.get("contact_type_id"))),
        ("Owner", _lookup(lookups.users, person.get("assignee_id"))),
        ("Tags", _text(person.get("tags"))),
        *_custom_fields(person, lookups),
    ]
    return _row(
        f"person:{person['id']}",
        title,
        fields,
        _text(person.get("details")),
        _url(lookups, "person", person["id"]),
    )


def render_opportunity(opportunity: dict, lookups: Lookups) -> dict:
    title = _text(opportunity.get("name")) or "Untitled opportunity"
    value = opportunity.get("monetary_value")
    company = _name(lookups, "company", opportunity.get("company_id")) or _text(
        opportunity.get("company_name")
    )
    fields = [
        ("Status", _text(opportunity.get("status"))),
        ("Pipeline", _lookup(lookups.pipelines, opportunity.get("pipeline_id"))),
        ("Stage", _lookup(lookups.stages, opportunity.get("pipeline_stage_id"))),
        (
            "Value",
            f"{_text(value)} {_text(opportunity.get('monetary_unit'))}".strip()
            if value not in (None, "")
            else "",
        ),
        ("Company", company),
        ("Primary contact", _name(lookups, "person", opportunity.get("primary_contact_id"))),
        ("Owner", _lookup(lookups.users, opportunity.get("assignee_id"))),
        ("Priority", _text(opportunity.get("priority"))),
        ("Close date", _text(opportunity.get("close_date"))),
        ("Source", _lookup(lookups.customer_sources, opportunity.get("customer_source_id"))),
        ("Loss reason", _lookup(lookups.loss_reasons, opportunity.get("loss_reason_id"))),
        ("Tags", _text(opportunity.get("tags"))),
        *_custom_fields(opportunity, lookups),
    ]
    return _row(
        f"opportunity:{opportunity['id']}",
        title,
        fields,
        _text(opportunity.get("details")),
        _url(lookups, "opportunity", opportunity["id"]),
    )


def render_activity(activity: dict, lookups: Lookups) -> dict:
    parent = activity.get("parent") if isinstance(activity.get("parent"), dict) else {}
    kind = _text(parent.get("type"))
    parent_name = _name(lookups, kind, parent.get("id"))
    activity_type = activity.get("type") if isinstance(activity.get("type"), dict) else {}
    type_name = _lookup(lookups.activity_types, activity_type.get("id")) or "Activity"
    title = f"{type_name} on {parent_name}" if parent_name else type_name
    fields = [
        ("Type", type_name),
        (kind.capitalize(), parent_name),
        ("By", _lookup(lookups.users, activity.get("user_id"))),
        ("Date", _date(activity.get("activity_date"))),
    ]
    return _row(
        f"activity:{activity['id']}",
        title,
        fields,
        _text(activity.get("details")),
        _url(lookups, kind, parent.get("id")),
    )


_RENDER = {
    "companies": render_company,
    "people": render_person,
    "opportunities": render_opportunity,
    "activities": render_activity,
}
# the parents an activity is synced for; leads, projects and tasks are not synced
_ACTIVITY_PARENTS = ("company", "person", "opportunity")


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


def _as_list(payload: Any, path: str) -> list:
    if not isinstance(payload, list):
        raise CopperAPIError(f"Copper sent no list for {_endpoint(path)}")
    return payload


def _ids_to_names(records: list) -> dict[Any, str]:
    return {r["id"]: _text(r.get("name")) for r in records if isinstance(r, dict) and "id" in r}


class _Walker:
    """One run over the selected kinds, writing progress into ``state``.

    Copper has no delete feed and only opportunities can be filtered by modified
    date, so every cycle lists each kind in full and emits a row only when its
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
        max_requests: int,
        check_active: Callable[[], None] | None,
    ):
        self.client = client
        self.state = state
        self.stats = stats
        for key in ("scanned", "skipped", "deleted", "failed"):
            stats.setdefault(key, 0)
        self.include = include
        self.max_requests = max_requests
        self.check_active = check_active
        self.requests = 0
        self.emitted = 0
        start_run = getattr(client, "start_run", None)
        if callable(start_run):
            start_run()
        self.walks = [kind for kind in KINDS if kind in include]
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
    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        if self.check_active is not None:
            self.check_active()
        if self.requests >= self.max_requests:
            raise _CutShortError("budget")
        self.requests += 1
        try:
            return self.client.request(method, path, body=body, check_active=self.check_active)
        except CopperRateLimitedError:
            raise _CutShortError("rate_limit") from None
        except CopperAuthError:
            if self.emitted:
                raise _CutShortError("auth") from None
            raise

    # -- lookups -----------------------------------------------------------
    def _load_lookups(self) -> None:
        account = self._call("GET", "/account")
        if not isinstance(account, dict) or account.get("id") is None:
            raise CopperAPIError("Copper sent no account for /account")
        self._check_account(account["id"])
        self.lookups.account_id = account["id"]
        self.lookups.users = _ids_to_names(list(self._search_all("/users/search")))
        definitions = _as_list(
            self._call("GET", "/custom_field_definitions"), "/custom_field_definitions"
        )
        self.lookups.fields = {
            d["id"]: (
                _text(d.get("name")),
                _text(d.get("data_type")),
                _ids_to_names(d.get("options") or []),
            )
            for d in definitions
            if isinstance(d, dict) and "id" in d
        }
        if set(self.include) & {"companies", "people"}:
            path = "/contact_types"
            self.lookups.contact_types = _ids_to_names(_as_list(self._call("GET", path), path))
        if "opportunities" in self.include:
            pipelines = _as_list(self._call("GET", "/pipelines"), "/pipelines")
            self.lookups.pipelines = _ids_to_names(pipelines)
            self.lookups.stages = _ids_to_names(
                [s for p in pipelines if isinstance(p, dict) for s in p.get("stages") or []]
            )
            for attr, path in (
                ("customer_sources", "/customer_sources"),
                ("loss_reasons", "/loss_reasons"),
            ):
                setattr(self.lookups, attr, _ids_to_names(_as_list(self._call("GET", path), path)))
        if "activities" in self.include:
            types = self._call("GET", "/activity_types")
            if not isinstance(types, dict):
                raise CopperAPIError("Copper sent no activity types for /activity_types")
            # only user activities (notes, calls, meetings and custom types) carry content;
            # system ones log field changes that the record documents already show
            self.lookups.activity_types = _ids_to_names(
                [t for t in types.get("user") or [] if not t.get("is_disabled")]
            )

    def _check_account(self, account_id: Any) -> None:
        """Refuse state written for another account, which would forget that account's rows."""
        stored = self.state.setdefault("account_id", account_id)
        if stored != account_id:
            raise CopperSourceError(
                "This resource_name already holds another Copper account's sync; "
                "give each Copper account its own resource_name"
            )

    def _search_all(self, path: str) -> Iterator[dict]:
        """Every record of a small lookup search, read in one go."""
        page = 1
        while True:
            records = _as_list(
                self._call("POST", path, {"page_size": PAGE_SIZE, "page_number": page}), path
            )
            yield from (r for r in records if isinstance(r, dict))
            if len(records) < PAGE_SIZE:
                return
            page += 1

    # -- listing -----------------------------------------------------------
    def _search_body(self, kind: str) -> dict[str, Any]:
        body: dict[str, Any] = {"page_size": PAGE_SIZE}
        if kind == "activities":
            body["activity_types"] = [
                {"id": type_id, "category": "user"}
                for type_id in sorted(self.lookups.activity_types)
            ]
        else:
            # oldest first, so records added during a walk land on its last page
            body.update(sort_by="date_created", sort_direction="asc")
        return body

    def _walk(self, kind: str) -> Iterator[dict]:
        """Yield every record of a kind, resuming at the page a cut run stopped on."""
        path = f"/{kind}/search"
        body = self._search_body(kind)
        walks = self.state.setdefault("walks", {})
        progress = walks.setdefault(kind, {"page": 1})
        if progress.get("done"):
            return
        if kind == "activities" and not body["activity_types"]:
            progress["done"] = True
            return
        while True:
            records = _as_list(
                self._call("POST", path, {**body, "page_number": progress["page"]}), path
            )
            yield from records
            if len(records) < PAGE_SIZE:
                break
            if progress["page"] * PAGE_SIZE >= SEARCH_CAP:
                progress["capped"] = True
                break
            progress["page"] += 1
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

    def _rows_of(self, kind: str) -> Iterator[dict]:
        render = _RENDER[kind]
        for record in self._walk(kind):
            if not isinstance(record, dict) or record.get("id") is None:
                self._failed("listing", CopperAPIError("a listed record has no id"))
                continue
            try:
                if kind == "activities" and not self._activity_in_scope(record):
                    continue
                row = render(record, self.lookups)
            except Exception as exc:  # one odd record must not block the whole account
                self._failed("render", CopperAPIError(type(exc).__name__))
                continue
            if kind != "activities":
                self.lookups.names[row["id"]] = _text(record.get("name"))
            fingerprint = _hash(row)
            if self._changed(row["id"], fingerprint):
                yield self._emit(row, fingerprint)

    def _activity_in_scope(self, activity: dict) -> bool:
        parent = activity.get("parent")
        activity_type = activity.get("type")
        return (
            isinstance(parent, dict)
            and parent.get("type") in _ACTIVITY_PARENTS
            and _SINGULAR_TO_KIND[parent["type"]] in self.include
            and isinstance(activity_type, dict)
            and activity_type.get("category") == "user"
            and activity_type.get("id") in self.lookups.activity_types
        )

    def _failed(self, step: str, exc: Exception) -> None:
        self.stats["failed"] += 1
        # the message names the status and endpoint, never the record id
        logger.warning("Copper %s failed for one record, retried next run: %s", step, exc)

    # -- forget-on-delete --------------------------------------------------
    def _close_cycle(self) -> None:
        """Queue ids not seen in a complete cycle, then start a fresh one."""
        walks = self.state.get("walks") or {}
        if any(progress.get("capped") for progress in walks.values()):
            # a capped listing is incomplete, so nothing missing can be trusted as deleted
            self.stats["failed"] = max(1, self.stats.get("failed", 0))
            self.stats["failed_cap"] = 1
            logger.warning(
                "Copper search returns at most %d records per kind; deletions are not "
                "detected for an account larger than that.",
                SEARCH_CAP,
            )
        else:
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
            except (CopperAPIError, CopperAccessError) as exc:
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
        kind = _SINGULAR_TO_KIND[singular]
        if kind not in self.include:
            return True  # deselected kinds leave memory
        try:
            record = self._call("GET", f"/{kind}/{record_id}")
        except CopperNotFoundError:
            return True
        if not isinstance(record, dict) or record.get("id") is None:
            raise CopperAPIError(f"Copper sent no {singular} for /{kind}/{{id}}")
        # an activity whose type was disabled, or whose parent kind was deselected, goes;
        # anything else is still there: a listing that shifted under us, seen next cycle
        return kind == "activities" and not self._activity_in_scope(record)

    # -- the run -----------------------------------------------------------
    def rows(self) -> Iterator[dict]:
        self._load_lookups()
        # finish deletions a cut run left behind before starting a new listing
        yield from self._forget()
        for kind in self.walks:
            yield from self._rows_of(kind)
        self._close_cycle()
        yield from self._forget()


_SINGULAR_TO_KIND = {singular: kind for kind, singular in _SINGULAR.items()}


def _iter_rows(
    client: Any,
    state: dict,
    stats: dict[str, int],
    *,
    include: tuple[str, ...] = KINDS,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict]:
    """Yield the changed rows of an account. Pure of dlt, so tests drive it with a dict."""
    walker = _Walker(
        client,
        state,
        stats,
        include=include,
        max_requests=max_requests,
        check_active=check_active,
    )
    try:
        yield from walker.rows()
    except _CutShortError as cut:
        stats["failed"] = max(1, stats.get("failed", 0))
        stats[f"failed_{cut.reason}"] = 1
        logger.warning("Copper sync stopped early (%s); it resumes on the next run.", cut.reason)


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def copper_source(
    *,
    resource_name: str,
    api_key: str | None = None,
    user_email: str | None = None,
    service: Any = None,
    check_active: Callable[[], None] | None = None,
    include: tuple[str, ...] = KINDS,
    max_requests: int = DEFAULT_MAX_REQUESTS,
):
    """Return a ``dlt`` resource that yields a Copper account's documents for ``remember``.

    Args:
        resource_name: Stable name of this connection, one per Copper account. It scopes
            the sync state and the cleanup of deleted records, so keep it fixed across
            runs. A run refuses state written for another account.
        api_key: Copper API key. Used with ``user_email`` to build the client when
            ``service`` is omitted.
        user_email: Email of the Copper user who created the API key. Copper rejects
            a key sent with any other email.
        service: Pre-built client, mainly an injection point for tests.
        check_active: Optional host authorization checkpoint, called before each
            request.
        include: Which of ``"companies"``, ``"people"``, ``"opportunities"`` and
            ``"activities"`` to sync. Activities are synced for the selected kinds only.
        max_requests: Request budget of one run. A bigger account finishes over
            several runs.

    Returns:
        A ``dlt`` resource configured with ``primary_key="id"``,
        ``write_disposition="merge"`` and a ``_deleted`` hard-delete column.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError("Copper sync requires cognee>=1.6.3 (table-scoped document cleanup).")
    if (
        not isinstance(resource_name, str)
        or not resource_name.strip()
        or len(resource_name) > _MAX_NAME_LENGTH
    ):
        raise ValueError(
            f"copper_source needs a resource_name of 1 to {_MAX_NAME_LENGTH} characters, "
            "one per Copper account"
        )
    if service is None:
        # the email is the part people forget, so name exactly what is missing
        missing = [
            name for name, value in (("api_key", api_key), ("user_email", user_email)) if not value
        ]
        if missing:
            raise ValueError(
                f"copper_source needs a service, or both api_key and user_email "
                f"(missing: {', '.join(missing)})"
            )
    include = tuple(include)
    if not include or set(include) - set(KINDS):
        raise ValueError(f"include must be a non-empty subset of {KINDS}")
    if include == ("activities",):
        raise ValueError(
            "activities are synced for companies, people or opportunities; "
            "include at least one of them"
        )
    if not isinstance(max_requests, int) or max_requests < 1:
        raise ValueError("max_requests must be a positive integer")

    # built here so a malformed key fails at construction and the closure holds no raw key
    client = service if service is not None else CopperClient(api_key or "", user_email or "")
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def copper_account():
        stats.clear()
        stats.update(scanned=0, skipped=0, deleted=0, failed=0)
        rows = _iter_rows(
            client,
            dlt.current.resource_state(),
            stats,
            include=include,
            max_requests=max_requests,
            check_active=check_active,
        )
        yield from dlt_utils.guarded_rows(rows, check_active)

    resource = copper_account()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "copper")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    resource.cognee_sync_stats = stats
    return resource
