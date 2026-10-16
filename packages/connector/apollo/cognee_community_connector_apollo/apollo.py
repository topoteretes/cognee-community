"""Apollo.io connector for cognee: a dlt resource that syncs your Apollo CRM into memory.

It ingests your team's own contacts, accounts and sequence activity, never
Apollo's enrichment database, and is meant to be handed to ``cognee.remember``::

    await cognee.remember(
        apollo_source(api_key="<apollo api key>", resource_name="apollo_sales"),
        dataset_name="apollo",
        primary_key="id",
        write_disposition="merge",  # required, the add pipeline defaults to "replace"
    )
"""

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("apollo_connector")

BASE_URL = "https://api.apollo.io/api/v1"
APP_URL = "https://app.apollo.io/#"
KINDS = ("contacts", "accounts", "sequences")
PAGE_SIZE = 100
# apollo search stops at 500 pages of 100 records
SEARCH_CAP = 50_000
# one capped listing (500 pages) per run; the rate-limit windows stop smaller plans first
DEFAULT_MAX_REQUESTS = 500
# requests left untouched in each rate-limit window, for the key's other users
_REQUESTS_RESERVE = 10
# the activity feed has no paging, so a contact keeps at most its latest 50 events
_ACTIVITY_LIMIT = 50
_TIMEOUT = (10, 30)
_RETRIES = 3
_RATE_HEADERS = {
    "minute": "x-minute-requests-left",
    "hourly": "x-hourly-requests-left",
    "daily": "x-24-hour-requests-left",
}
# legacy 422 texts seen live for a deleted or unknown contact or account, matched whole
# so a record name can't trip them; error_details.code replaces them on 16 feb 2027
_GONE_MESSAGE = re.compile(r"^(oops! )?this (contact|account) (has been deleted|does not exist)")
_GONE_REASONS = ("NOT_FOUND", "DELETED")


class ApolloSourceError(RuntimeError):
    """Base class of the errors this source raises."""


class ApolloAPIError(ApolloSourceError):
    """Apollo answered with an error, or could not be reached."""


class ApolloAuthError(ApolloSourceError):
    """Apollo rejected the API key (HTTP 401)."""


class ApolloAccessError(ApolloSourceError):
    """The API key or plan cannot call an endpoint (HTTP 403)."""


class ApolloRateLimitedError(ApolloSourceError):
    """Apollo answered HTTP 429."""


class ApolloNotFoundError(ApolloAPIError):
    """The record was deleted or never existed."""


def _int_header(headers: Any, name: str) -> int | None:
    try:
        return int(headers.get(name))
    except (TypeError, ValueError):
        return None


def _is_gone(body: Any) -> bool:
    """Whether Apollo said the record is gone. Only matched, never echoed."""
    if not isinstance(body, dict):
        return False
    details = body.get("error_details")
    code = details.get("code") if isinstance(details, dict) else None
    if isinstance(code, str) and code:
        # codes read DOMAIN.CATEGORY.REASON; apollo documents no deleted-record code yet
        reason = code.rsplit(".", 1)[-1].upper()
        return any(marker in reason for marker in _GONE_REASONS)
    texts = (body.get("error"), body.get("message"))
    return any(isinstance(text, str) and _GONE_MESSAGE.match(text.lower()) for text in texts)


def _endpoint(path: str) -> str:
    """The path with a record id replaced, so error messages never carry one."""
    head, _, tail = path.rpartition("/")
    if head in ("/contacts", "/accounts") and tail != "search":
        return f"{head}/{{id}}"
    return path


class ApolloClient:
    """Minimal synchronous REST client for Apollo.

    After every successful call ``rate_limit`` holds the requests left in the minute,
    hourly and daily windows. Tests can pass any object with the same ``request``
    method and ``rate_limit`` attribute instead.
    """

    def __init__(self, api_key: str, *, sleep: Callable[[float], None] = time.sleep):
        key = (api_key or "").strip()
        # reject early and without echoing the key: requests would put it in its exception
        if not key or not key.isascii() or not key.isprintable() or " " in key:
            raise ValueError("The Apollo API key is empty or has an invalid format")
        self._api_key = key
        self._sleep = sleep
        self.rate_limit: dict[str, int | None] = {}

    def __repr__(self) -> str:
        return "ApolloClient(api_key=<redacted>)"

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        headers = {
            "x-api-key": self._api_key,
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
        }
        for attempt in range(_RETRIES):
            last = attempt == _RETRIES - 1
            try:
                response = requests.request(
                    method,
                    BASE_URL + path,
                    json=body,
                    params=params,
                    headers=headers,
                    timeout=_TIMEOUT,
                )
            except requests.RequestException as exc:
                if last:
                    raise ApolloAPIError(f"Apollo request failed: {type(exc).__name__}") from None
                self._sleep(2**attempt)
                continue
            if response.status_code >= 500 and not last:
                self._sleep(2**attempt)
                continue
            return self._parse(response, path)

    def _parse(self, response: Any, path: str) -> Any:
        status = response.status_code
        if status == 401:
            raise ApolloAuthError("Apollo rejected the API key (HTTP 401)")
        if status == 403:
            raise ApolloAccessError(
                f"The Apollo API key or plan cannot call {_endpoint(path)} (HTTP 403)"
            )
        if status == 429:
            raise ApolloRateLimitedError("Apollo rate limit exceeded (HTTP 429)")
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if status == 404 or (status == 422 and _is_gone(payload)):
            raise ApolloNotFoundError(f"Apollo record not found (HTTP {status})")
        if status != 200:
            raise ApolloAPIError(f"Apollo request failed: HTTP {status}")
        # every endpoint used answers with an object, except /labels with a list
        if not isinstance(payload, dict | list):
            raise ApolloAPIError(f"Apollo sent an unreadable answer for {_endpoint(path)}")
        headers = response.headers
        self.rate_limit = {
            window: _int_header(headers, name) for window, name in _RATE_HEADERS.items()
        }
        return payload


# ---------------------------------------------------------------------------
# Rendering: deterministic, first-party fields only, nothing volatile.
# ---------------------------------------------------------------------------
@dataclass
class Lookups:
    """Id-to-name maps that turn Apollo ids into readable text."""

    contact_stages: dict[str, str] = field(default_factory=dict)
    account_stages: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    fields: dict[str, str] = field(default_factory=dict)
    sequences: dict[str, str] = field(default_factory=dict)


def _text(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(sorted(_text(item) for item in value if _text(item)))
    return "" if value is None else str(value).strip()


def _lines(fields: list[tuple[str, str]]) -> list[str]:
    return [f"{label}: {value}" for label, value in fields if value]


def _names(ids: Any, lookup: dict[str, str]) -> str:
    return ", ".join(sorted({lookup[i] for i in ids or [] if lookup.get(i)}))


def _custom_fields(values: Any, lookup: dict[str, str]) -> list[tuple[str, str]]:
    if not isinstance(values, dict):
        return []
    pairs = ((lookup.get(field_id, ""), _text(value)) for field_id, value in values.items())
    return sorted((label, text) for label, text in pairs if label and text)


def render_contact(
    contact: dict[str, Any], lookups: Lookups, events: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    name = _text(contact.get("name")) or _text(
        f"{contact.get('first_name') or ''} {contact.get('last_name') or ''}"
    )
    title = name or "Unnamed contact"
    sequences = sorted(
        f"{lookups.sequences.get(status.get('emailer_campaign_id'), 'Unknown sequence')}"
        f" ({_text(status.get('status')) or 'unknown'})"
        for status in contact.get("contact_campaign_statuses") or []
    )
    fields = [
        ("Job title", _text(contact.get("title"))),
        ("Company", _text(contact.get("organization_name"))),
        ("Stage", lookups.contact_stages.get(contact.get("contact_stage_id"), "")),
        ("Lists", _names(contact.get("label_ids"), lookups.labels)),
        *_custom_fields(contact.get("typed_custom_fields"), lookups.fields),
        ("Sequences", "; ".join(sequences)),
    ]
    parts = _lines(fields)
    ordered = sorted(
        events or [],
        key=lambda e: (
            str(e.get("occurred_at") or ""),
            str(e.get("type")),
            str(e.get("sequence_id")),
        ),
    )
    activity = [
        f"{str(event.get('occurred_at') or '')[:10]} {_text(event.get('type'))}: "
        f"{lookups.sequences.get(event.get('sequence_id')) or _text(event.get('sequence_name'))}"
        for event in ordered
    ]
    if activity:
        cut = f" (cut at {_ACTIVITY_LIMIT} events)" if len(activity) >= _ACTIVITY_LIMIT else ""
        parts.extend(["", f"Sequence activity{cut}:", *activity])
    return {
        "id": f"contact:{contact['id']}",
        "title": title,
        "content": "\n".join(parts).strip() or title,
        "url": f"{APP_URL}/contacts/{contact['id']}",
        "_deleted": False,
    }


def render_account(account: dict[str, Any], lookups: Lookups) -> dict[str, Any]:
    title = _text(account.get("name")) or _text(account.get("domain")) or "Unnamed account"
    fields = [
        ("Domain", _text(account.get("domain"))),
        ("Stage", lookups.account_stages.get(account.get("account_stage_id"), "")),
        ("Lists", _names(account.get("label_ids"), lookups.labels)),
        *_custom_fields(account.get("typed_custom_fields"), lookups.fields),
    ]
    return {
        "id": f"account:{account['id']}",
        "title": title,
        "content": "\n".join(_lines(fields)) or title,
        "url": f"{APP_URL}/accounts/{account['id']}",
        "_deleted": False,
    }


def render_sequence(sequence: dict[str, Any]) -> dict[str, Any]:
    title = _text(sequence.get("name")) or "Untitled sequence"
    if sequence.get("archived"):
        status = "archived"
    else:
        status = "active" if sequence.get("active") else "inactive"
    fields = [("Status", status), ("Steps", _text(sequence.get("num_steps")))]
    return {
        "id": f"sequence:{sequence['id']}",
        "title": title,
        "content": "\n".join(_lines(fields)),
        "url": f"{APP_URL}/sequences/{sequence['id']}",
        "_deleted": False,
    }


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _total_pages(data: dict) -> int:
    """The page count of a search answer; without it a listing can't be trusted as complete."""
    pagination = data.get("pagination")
    total = pagination.get("total_pages") if isinstance(pagination, dict) else None
    if not isinstance(total, int):
        raise ApolloAPIError("Apollo search answered without pagination")
    return total


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
class _CutShortError(Exception):
    """Internal: stop the run here, keeping the state reached so far."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


_SEARCHES = {
    "contacts": ("/contacts/search", "contact"),
    "accounts": ("/accounts/search", "account"),
}


class _Walker:
    """One run over the selected kinds, writing progress into ``state``.

    Apollo has no change feed and no deleted flag, so every cycle lists each kind
    in full (oldest first) and emits a row only when its fingerprint changed. Ids
    that stopped appearing are re-checked one by one before they are forgotten.
    """

    def __init__(
        self,
        client: Any,
        state: dict,
        stats: dict[str, int],
        *,
        include: tuple[str, ...],
        filters: dict[str, list[str]],
        max_requests: int,
        check_active: Callable[[], None] | None,
    ):
        self.client = client
        self.state = state
        self.stats = stats
        for key in ("scanned", "skipped", "deleted", "failed"):
            stats.setdefault(key, 0)
        self.include = include
        self.filters = filters
        self.max_requests = max_requests
        self.check_active = check_active
        self.requests = 0
        self.emitted = 0
        self.fingerprints: dict[str, str] = state.setdefault("fingerprints", {})
        self.seen: list[str] = state.setdefault("seen", [])
        self.lookups = Lookups()

    # -- requests ----------------------------------------------------------
    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        if self.check_active is not None:
            self.check_active()
        self._check_budget()
        self.requests += 1
        try:
            return self.client.request(method, path, **kwargs)
        except ApolloRateLimitedError:
            raise _CutShortError("rate_limit") from None
        except ApolloAuthError:
            if self.emitted:
                raise _CutShortError("auth") from None
            raise

    def _check_budget(self) -> None:
        if self.requests >= self.max_requests:
            raise _CutShortError("budget")
        remaining = getattr(self.client, "rate_limit", None) or {}
        for window in _RATE_HEADERS:
            left = remaining.get(window)
            if left is not None and left < _REQUESTS_RESERVE:
                raise _CutShortError("rate_limit")

    # -- lookups -----------------------------------------------------------
    def _load_lookups(self) -> list[dict]:
        """Load the id-to-name maps and return the full sequence list."""
        contact_stages = self._call("GET", "/contact_stages").get("contact_stages") or []
        account_stages = self._call("GET", "/account_stages").get("account_stages") or []
        self._check_team(contact_stages + account_stages)
        # /labels answers with a bare list
        labels = self._call("GET", "/labels") or []
        fields = self._call("GET", "/fields", params={"source": "custom"}).get("fields") or []
        sequences = list(self._all_sequences())
        self.lookups = Lookups(
            contact_stages={s["id"]: _text(s.get("name")) for s in contact_stages},
            account_stages={s["id"]: _text(s.get("name")) for s in account_stages},
            labels={label["id"]: _text(label.get("name")) for label in labels},
            # /fields ids look like "contact.<id>"; records key custom values by the bare id
            fields={f["id"].split(".", 1)[-1]: _text(f.get("label")) for f in fields},
            sequences={s["id"]: _text(s.get("name")) for s in sequences},
        )
        return sequences

    def _check_team(self, stages: list[dict]) -> None:
        """Refuse state written for another Apollo team, which would forget that team's rows."""
        team_id = next((s["team_id"] for s in stages if s.get("team_id")), None)
        if team_id is None:
            return
        stored = self.state.setdefault("team_id", team_id)
        if stored != team_id:
            raise ApolloSourceError(
                "This resource_name already holds another Apollo team's sync; "
                "give each Apollo account its own resource_name"
            )

    def _all_sequences(self) -> Iterator[dict]:
        page = 1
        while True:
            # the openapi lists these as query params, but apollo reads the json body too
            data = self._call(
                "POST", "/emailer_campaigns/search", body={"page": page, "per_page": PAGE_SIZE}
            )
            records = data.get("emailer_campaigns") or []
            total_pages = _total_pages(data)
            yield from records
            if len(records) < PAGE_SIZE or page >= total_pages:
                return
            page += 1

    # -- listing -----------------------------------------------------------
    def _search_body(self, kind: str) -> dict[str, Any]:
        _, singular = _SEARCHES[kind]
        body: dict[str, Any] = {
            "sort_by_field": f"{singular}_created_at",
            "sort_ascending": True,
            "per_page": PAGE_SIZE,
        }
        for key in (f"{singular}_stage_ids", f"{singular}_label_ids"):
            if self.filters.get(key):
                body[key] = self.filters[key]
        return body

    def _walk(self, kind: str) -> Iterator[dict]:
        """Yield every record of a kind, resuming at the page a cut run stopped on."""
        path, _ = _SEARCHES[kind]
        body = self._search_body(kind)
        walks = self.state.setdefault("walks", {})
        query = _hash(body)
        # a page reached under other filters points into a different listing
        if walks.get(kind, {}).get("query") != query:
            walks[kind] = {"page": 1, "query": query}
        progress = walks[kind]
        if progress.get("done"):
            return
        while True:
            data = self._call("POST", path, body={**body, "page": progress["page"]})
            records = data.get(kind) or []
            total_pages = _total_pages(data)
            if (data["pagination"].get("total_entries") or 0) > SEARCH_CAP:
                progress["capped"] = True
            yield from records
            last_page = min(total_pages, SEARCH_CAP // PAGE_SIZE)
            if len(records) < PAGE_SIZE or progress["page"] >= last_page:
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

    # -- kinds -------------------------------------------------------------
    def _sequence_rows(self, sequences: list[dict]) -> Iterator[dict]:
        for sequence in sequences:
            row = render_sequence(sequence)
            fingerprint = _hash(row)
            if self._changed(row["id"], fingerprint):
                yield self._emit(row, fingerprint)

    def _account_rows(self) -> Iterator[dict]:
        for account in self._walk("accounts"):
            row = render_account(account, self.lookups)
            fingerprint = _hash(row)
            if self._changed(row["id"], fingerprint):
                yield self._emit(row, fingerprint)

    def _contact_rows(self) -> Iterator[dict]:
        for contact in self._walk("contacts"):
            row = render_contact(contact, self.lookups)
            statuses = contact.get("contact_campaign_statuses") or []
            # sequence events don't bump updated_at, so raw statuses and the last
            # activity date decide when the activity feed is read again
            activity_key = (
                _hash([sorted(map(_hash, statuses)), contact.get("last_activity_date")])
                if statuses
                else ""
            )
            fingerprint = _hash(row) + activity_key
            if not self._changed(row["id"], fingerprint):
                continue
            if statuses:
                try:
                    events = self._activity(contact["id"])
                except (ApolloAPIError, ApolloAccessError) as exc:
                    # no fingerprint is stored, so the next run reads this contact again
                    self._failed("activity feed", exc)
                    continue
                row = render_contact(contact, self.lookups, events)
            yield self._emit(row, fingerprint)

    def _activity(self, contact_id: str) -> list[dict]:
        try:
            data = self._call(
                "POST",
                "/emailer_campaigns/activity_feed",
                body={"contact_id": contact_id, "per_page": _ACTIVITY_LIMIT},
            )
        except ApolloNotFoundError:
            return []  # deleted meanwhile; the next cycle forgets it
        events = data.get("events") or []
        if len(events) >= _ACTIVITY_LIMIT:
            logger.warning(
                "Apollo returned %d activity events for a contact and has no paging; "
                "older events are left out.",
                _ACTIVITY_LIMIT,
            )
        return events

    def _failed(self, step: str, exc: Exception) -> None:
        self.stats["failed"] += 1
        # the message names the status and endpoint, never the record id
        logger.warning("Apollo %s failed for one record, retried next run: %s", step, exc)

    # -- forget-on-delete --------------------------------------------------
    def _close_cycle(self) -> None:
        """Queue ids not seen in a complete cycle, then start a fresh one."""
        walks = self.state.get("walks") or {}
        capped = any(progress.get("capped") for progress in walks.values())
        if capped:
            # a capped listing is incomplete, so nothing missing can be trusted as deleted
            self.stats["failed"] = max(1, self.stats.get("failed", 0))
            self.stats["failed_cap"] = 1
            logger.warning(
                "Apollo search is capped at %d records; narrow the sync with stage or list "
                "filters. Deletions are not detected until then.",
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
            except (ApolloAPIError, ApolloAccessError) as exc:
                # keep the row; its fingerprint queues it again when this cycle closes
                self._failed("deletion check", exc)
                gone = False
            if gone:
                self.fingerprints.pop(row_id, None)
                self.stats["deleted"] += 1
                yield {"id": row_id, "_deleted": True}
            pending.pop(0)

    def _gone(self, row_id: str) -> bool:
        kind, record_id = row_id.split(":", 1)
        if f"{kind}s" not in self.include:
            return True  # deselected kinds leave memory
        if kind == "sequence":
            return True  # the sequence list is read in full every run
        try:
            record = self._call("GET", f"/{kind}s/{record_id}").get(kind)
        except ApolloNotFoundError:
            return True
        if not isinstance(record, dict):
            raise ApolloAPIError(f"Apollo sent no {kind} for /{kind}s/{{id}}")
        # still in apollo: forget it only if it left the selected stages or lists
        return not self._in_scope(kind, record)

    def _in_scope(self, kind: str, record: dict) -> bool:
        stage_ids = self.filters.get(f"{kind}_stage_ids")
        label_ids = self.filters.get(f"{kind}_label_ids")
        if stage_ids and record.get(f"{kind}_stage_id") not in stage_ids:
            return False
        if not label_ids:
            return True
        return bool(set(record.get("label_ids") or []) & set(label_ids))

    # -- the run -----------------------------------------------------------
    def rows(self) -> Iterator[dict]:
        sequences = self._load_lookups()
        # finish deletions a cut run left behind before starting a new listing
        yield from self._forget()
        if "sequences" in self.include:
            yield from self._sequence_rows(sequences)
        if "accounts" in self.include:
            yield from self._account_rows()
        if "contacts" in self.include:
            yield from self._contact_rows()
        self._close_cycle()
        yield from self._forget()


def _iter_rows(
    client: Any,
    state: dict,
    stats: dict[str, int],
    *,
    include: tuple[str, ...] = KINDS,
    filters: dict[str, list[str]] | None = None,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict]:
    """Yield the changed rows of a workspace. Pure of dlt, so tests drive it with a dict."""
    walker = _Walker(
        client,
        state,
        stats,
        include=include,
        filters=filters or {},
        max_requests=max_requests,
        check_active=check_active,
    )
    try:
        yield from walker.rows()
    except _CutShortError as cut:
        stats["failed"] = max(1, stats.get("failed", 0))
        stats[f"failed_{cut.reason}"] = 1
        logger.warning("Apollo sync stopped early (%s); it resumes on the next run.", cut.reason)


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def apollo_source(
    *,
    resource_name: str,
    api_key: str | None = None,
    service: Any = None,
    check_active: Callable[[], None] | None = None,
    include: tuple[str, ...] = KINDS,
    contact_stage_ids: list[str] | None = None,
    contact_label_ids: list[str] | None = None,
    account_stage_ids: list[str] | None = None,
    account_label_ids: list[str] | None = None,
    max_requests: int = DEFAULT_MAX_REQUESTS,
):
    """Return a ``dlt`` resource that yields an Apollo workspace's documents for ``remember``.

    Args:
        resource_name: Stable name of this connection, one per Apollo account. It scopes
            the sync state and the cleanup of deleted records, so keep it fixed across
            runs. There is no default: two accounts sharing a name would forget each
            other's records, and a run refuses state written for another Apollo team.
        api_key: Apollo API key. Used to build the client when ``service`` is omitted.
        service: Pre-built client, mainly an injection point for tests.
        check_active: Optional host authorization checkpoint, called before each
            request.
        include: Which of ``"contacts"``, ``"accounts"`` and ``"sequences"`` to sync.
        contact_stage_ids, contact_label_ids: Only sync contacts in these stages or lists.
        account_stage_ids, account_label_ids: Only sync accounts in these stages or lists.
        max_requests: Request budget of one run. A bigger workspace finishes over
            several runs.

    Returns:
        A ``dlt`` resource configured with ``primary_key="id"``,
        ``write_disposition="merge"`` and a ``_deleted`` hard-delete column.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError("Apollo sync requires cognee>=1.6.1 (table-scoped document cleanup).")
    if not isinstance(resource_name, str) or not resource_name.strip():
        raise ValueError("apollo_source needs a resource_name, one per Apollo account")
    if service is None and not api_key:
        raise ValueError("apollo_source needs a service or an api_key")
    include = tuple(include)
    if not include or set(include) - set(KINDS):
        raise ValueError(f"include must be a non-empty subset of {KINDS}")

    # built here so a malformed key fails at construction and the closure holds no raw key
    client = service if service is not None else ApolloClient(api_key or "")
    filters = {
        "contact_stage_ids": list(contact_stage_ids or []),
        "contact_label_ids": list(contact_label_ids or []),
        "account_stage_ids": list(account_stage_ids or []),
        "account_label_ids": list(account_label_ids or []),
    }
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def apollo_workspace():
        stats.clear()
        stats.update(scanned=0, skipped=0, deleted=0, failed=0)
        # the quota a reused client saw last run is stale once its windows reset
        client.rate_limit = {}
        rows = _iter_rows(
            client,
            dlt.current.resource_state(),
            stats,
            include=include,
            filters=filters,
            max_requests=max_requests,
            check_active=check_active,
        )
        yield from dlt_utils.guarded_rows(rows, check_active)

    resource = apollo_workspace()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "apollo")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    resource.cognee_sync_stats = stats
    return resource
