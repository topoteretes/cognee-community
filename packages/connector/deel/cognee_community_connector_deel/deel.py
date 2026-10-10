"""DLT source for Deel (contracts + worker directory metadata) with forget-on-delete.

Ingests *metadata* about Deel contracts and people as small Markdown documents, so
they flow through cognee's cognify pipeline like the other document connectors
(``cognee_document_source = "deel"``). Contract text and worker personal data are
sensitive HR data, so everything is opt-in and allowlisted (see "Privacy").

Design
------
* **HTTP** — the list endpoints are configured through dlt's declarative ``rest_api`` source
  (endpoint, Bearer auth, pagination, params); single calls (startup check, documents) use
  dlt's ``RESTClient``. Both share one session that retries 429/5xx with exponential backoff
  (``Retry-After`` honoured when present) behind a 5 req/s client-side throttle. There is no
  hand-rolled request loop.
* **One pass per run** — each resource pages through its list endpoint once. During
  that pass it emits only changed records *and* collects every id seen.
* **Change detection** — neither Deel list endpoint offers a server-side ``updated_at``
  filter (and the documented contracts schema lists no ``updated_at``, although the sandbox
  returns one), so a stable content hash of the allowlisted fields is kept in dlt resource
  state (``id -> "hash:first_seen_epoch"``). Unchanged records are not re-sent to cognify.
  When records carry ``updated_at`` the newest value is stored as a cursor, and records newer
  than ``cursor - overlap_seconds`` are re-emitted (``merge`` absorbs the overlap; their
  content is identical, so cognee does not re-process them).
* **Deletes** — ids known from earlier runs but absent from a *clean* pass are emitted
  as hard-delete tombstones (``_deleted=True`` on a ``merge`` resource). dlt drops the
  staging row and cognee's ``orphan_cleanup`` (``resolve_dlt_sources``) then removes the
  record from the graph, vector and relational stores. A pass that hit an error, a
  truncated page, a repeated page or an implausible (empty) response emits **no**
  tombstones. A pass that would remove more than ``max_delete_ratio`` of known records
  aborts the run unless ``force_delete=True``.
* **Write disposition** — ``merge`` on ``id`` is required for incremental runs: under
  ``replace`` every record that is not re-emitted is treated as deleted by cognee. The
  source therefore reads the *effective* disposition at run time and, if the caller
  forces ``replace`` (cognee's default!), switches to a full snapshot pass so nothing
  is wiped. ``full_reconcile=True`` forces the same full pass under ``merge``.

State size: ``known`` holds ~65 bytes per record (id + hash + epoch), i.e. roughly
0.7 MB for 10k records; dlt stores state compressed in the destination.

Privacy
-------
Only an explicit allowlist of fields per resource is ever read; new API fields never
flow into documents automatically. Email / name / address are dropped unless
``include_pii=True``. Compensation, birth dates and government/tax ids are never
included. Contract documents are fetched only with ``include_contract_documents=True``.
Credentials and record content are never logged; log lines carry counts only (plus
opaque ids for skipped records).
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("deel_connector")

# ===========================================================================
# Constants
# ===========================================================================

DEEL_SOURCE_NAME = "deel"
CONTRACTS_TABLE = "deel_contracts"
PEOPLE_TABLE = "deel_people"
DOCUMENTS_TABLE = "deel_contract_documents"
SKIPPED_TABLE = "deel_skipped"

# Documented at https://developer.deel.com/api/stable/sandbox.md (the endpoint pages call
# it "Demo"). Not yet confirmed with a live call from this connector.
DEEL_PRODUCTION_URL = "https://api.letsdeel.com/rest"
DEEL_SANDBOX_URL = "https://api-staging.letsdeel.com/rest"

TOKEN_ENV = "DEEL_API_TOKEN"
BASE_URL_ENV = "DEEL_BASE_URL"

# Best-practices page: "maximum page size of 100 records per request".
MAX_PAGE_SIZE = 100
# Rate limit: 5 requests/second per organisation, no rate-limit headers.
_MIN_REQUEST_INTERVAL = 0.21
_MAX_ATTEMPTS = 5
_ALL_RESOURCES = ("contracts", "people")

# Documented only for EOR contracts, and it returns metadata, not file content.
DEFAULT_DOCUMENTS_PATH = "/eor/contracts/{contract_id}/documents"
DEFAULT_DOCUMENT_TYPES = ("text/plain", "text/markdown", "text/csv", "application/pdf")

_HASH_LEN = 16


# ===========================================================================
# Errors
# ===========================================================================


class DeelError(Exception):
    """Base error for the Deel connector. Messages never contain tokens or record content."""


class DeelAuthError(DeelError):
    """The token was rejected (401) or lacks a required scope (403)."""


class DeelDeleteGuardError(DeelError):
    """A sync would delete more than ``max_delete_ratio`` of the known records."""


class _BadRecord(Exception):  # noqa: N818 - internal control-flow signal, not a public error
    """A single malformed record; carries a fixed-vocabulary reason (never content)."""

    def __init__(self, reason: str, record_id: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.record_id = record_id


# ===========================================================================
# Configuration
# ===========================================================================


@dataclass(frozen=True)
class _Config:
    resources: tuple[str, ...]
    contract_statuses: tuple[str, ...]
    contract_types: tuple[str, ...]
    include_pii: bool
    include_contract_documents: bool
    document_max_bytes: int
    document_types: tuple[str, ...]
    documents_path: str
    page_size: int
    overlap_seconds: int
    full_reconcile: bool
    max_delete_ratio: float
    force_delete: bool
    drop_statuses: tuple[str, ...]
    skipped_table: bool


def _resolve_token(token: str | None) -> str:
    """Token from the argument, ``DEEL_API_TOKEN`` or dlt secrets (never logged)."""
    resolved = token or os.environ.get(TOKEN_ENV)
    if not resolved:
        try:
            import dlt

            resolved = dlt.secrets.get("sources.deel.api_token")
        except Exception:  # secrets provider unavailable / key missing
            resolved = None
    if not resolved:
        raise ValueError(
            f"Deel API token required: pass token=, set {TOKEN_ENV}, or set "
            "sources.deel.api_token in dlt secrets."
        )
    return str(resolved)


# ===========================================================================
# HTTP layer (dlt RESTClient + throttle + pagination guards)
# ===========================================================================


def _build_session(
    *,
    max_attempts: int = _MAX_ATTEMPTS,
    backoff_factor: float = 1.0,
    max_delay: float = 30.0,
    min_interval: float = _MIN_REQUEST_INTERVAL,
    jitter: float = 0.05,
):
    """dlt retrying session (429/5xx, exponential backoff, Retry-After) + throttle.

    ``raise_for_status`` is off: RESTClient turns error statuses into ``HTTPError``
    after retries are exhausted. Deel returns no ``Retry-After``/rate-limit headers, so
    429s back off exponentially; the throttle keeps us under 5 req/s to avoid them.
    """
    from dlt.sources.helpers import requests as dlt_requests
    from requests.adapters import HTTPAdapter

    class _ThrottlingAdapter(HTTPAdapter):
        def __init__(self) -> None:
            super().__init__(pool_maxsize=10)
            self._lock = threading.Lock()
            self._ready_at = 0.0

        def send(self, request, **kwargs):
            with self._lock:
                wait = self._ready_at - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self._ready_at = time.monotonic() + min_interval + random.uniform(0, jitter)
            return super().send(request, **kwargs)

    client = dlt_requests.Client(
        raise_for_status=False,
        request_timeout=30,
        request_max_attempts=max_attempts,
        request_backoff_factor=backoff_factor,
        request_max_retry_delay=max_delay,
    )
    session = client.session
    adapter = _ThrottlingAdapter()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _paginator_base():
    from dlt.sources.helpers.rest_client.paginators import BasePaginator

    class _GuardedPaginator(BasePaginator):
        """Stops on empty or repeated pages and records ``page.total_rows``."""

        def __init__(self, limit: int, max_pages: int = 100_000) -> None:
            super().__init__()
            self.limit = limit
            self.max_pages = max_pages
            self.total: int | None = None
            self.repeated = False
            self._pages = 0
            self._prev_signature: str | None = None

        def init_request(self, request) -> None:
            request.params = dict(request.params or {})
            request.params["limit"] = self.limit

        def _stop_on_guard(self, response, data) -> bool:
            try:
                body = response.json()
            except ValueError:
                body = None
            page = body.get("page") if isinstance(body, dict) else None
            if isinstance(page, dict) and isinstance(page.get("total_rows"), int | float):
                self.total = int(page["total_rows"])
            self._pages += 1
            if not data:
                self._has_next_page = False
                return True
            signature = hashlib.sha256(
                "|".join(
                    str(rec.get("id")) if isinstance(rec, dict) else "?" for rec in data
                ).encode()
            ).hexdigest()
            if signature == self._prev_signature or self._pages >= self.max_pages:
                self.repeated = True
                self._has_next_page = False
                return True
            self._prev_signature = signature
            return False

    return _GuardedPaginator


def _cursor_paginator(limit: int):
    """Contracts: ``after_cursor`` in, ``page.cursor`` out."""
    base = _paginator_base()

    class _CursorPaginator(base):
        def __init__(self) -> None:
            super().__init__(limit)
            self._cursor: str | None = None
            self._seen_cursors: set[str] = set()

        def update_state(self, response, data=None) -> None:
            if self._stop_on_guard(response, data):
                return
            try:
                page = response.json().get("page") or {}
            except (ValueError, AttributeError):
                page = {}
            cursor = page.get("cursor")
            if not cursor:
                self._has_next_page = False
            elif cursor in self._seen_cursors:
                self.repeated = True
                self._has_next_page = False
            else:
                self._seen_cursors.add(cursor)
                self._cursor = cursor

        def update_request(self, request) -> None:
            if self._cursor:
                request.params["after_cursor"] = self._cursor

    return _CursorPaginator()


def _offset_paginator(limit: int):
    """People: ``offset``/``limit`` in, ``page.total_rows`` out."""
    base = _paginator_base()

    class _OffsetPaginator(base):
        def __init__(self) -> None:
            super().__init__(limit)
            self._offset = 0

        def init_request(self, request) -> None:
            super().init_request(request)
            request.params["offset"] = 0

        def update_state(self, response, data=None) -> None:
            if self._stop_on_guard(response, data):
                return
            self._offset += len(data)
            if self.total is not None:
                if self._offset >= self.total:
                    self._has_next_page = False
            elif len(data) < self.limit:
                self._has_next_page = False

        def update_request(self, request) -> None:
            request.params["offset"] = self._offset

    return _OffsetPaginator()


class _Api:
    """Thin Deel client over dlt's RESTClient."""

    def __init__(self, base_url: str, token: str, session: Any):
        from dlt.sources.helpers.rest_client import RESTClient
        from dlt.sources.helpers.rest_client.auth import BearerTokenAuth

        self._base_host = urlparse(base_url).netloc
        self._base_url, self._token, self._session = base_url, token, session
        self._client = RESTClient(
            base_url=base_url,
            auth=BearerTokenAuth(token=token),
            session=session,
            headers={"Accept": "application/json"},
        )

    def probe(self, path: str) -> int:
        """One cheap authenticated GET; returns the HTTP status."""
        return self._client.get(path, params={"limit": 1}).status_code

    def records(self, path: str, paginator: Any, params: dict[str, Any] | None = None):
        """Records of one list endpoint, via dlt's declarative ``rest_api`` source.

        Endpoint, bearer auth, pagination, params and data selector are all ``rest_api``
        config; the shared session adds retries and the client-side throttle.
        """
        from dlt.sources.rest_api import rest_api_resources

        endpoint: dict[str, Any] = {"path": path, "data_selector": "data", "paginator": paginator}
        if params:
            endpoint["params"] = params
        config = {
            "client": {
                "base_url": self._base_url,
                "auth": {"type": "bearer", "token": self._token},
                "session": self._session,
                "headers": {"Accept": "application/json"},
            },
            "resources": [{"name": "deel_list", "endpoint": endpoint}],
        }
        yield from rest_api_resources(config)[0]

    def get_json(self, path: str) -> Any:
        response = self._client.get(path)
        response.raise_for_status()
        return response.json()

    def download(self, url: str, allowed_types: Sequence[str], max_bytes: int):
        """Fetch a file; returns ``(reason, content_type, bytes)`` — reason set on skip.

        The bearer token is only attached for the Deel host, never for other hosts
        (e.g. pre-signed storage URLs).
        """
        from requests.auth import AuthBase

        class _NoAuth(AuthBase):
            def __call__(self, r):
                return r

        same_host = urlparse(url).netloc == self._base_host
        response = self._client.get(url, stream=True, **({} if same_host else {"auth": _NoAuth()}))
        try:
            if response.status_code >= 400:
                return f"download_http_{response.status_code}", None, b""
            ctype = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if ctype not in allowed_types:
                return "type_not_allowed", ctype, b""
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                return "too_large", ctype, b""
            buf = bytearray()
            for chunk in response.iter_content(65536):
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    return "too_large", ctype, b""
            return None, ctype, bytes(buf)
        finally:
            response.close()


# ===========================================================================
# Pass runner: one paginated sweep with completeness tracking
# ===========================================================================


class _Pass:
    """Iterates one list endpoint and records whether the sweep was complete."""

    def __init__(self, api: _Api, path: str, paginator: Any, params: dict[str, Any] | None):
        self._api, self._path, self._paginator, self._params = api, path, paginator, params
        self.fetched = 0
        self.complete = True
        self.reason: str | None = None

    def mark_incomplete(self, reason: str) -> None:
        if self.complete:
            self.complete, self.reason = False, reason

    def records(self) -> Iterator[Any]:
        from dlt.sources.helpers import requests as dlt_requests

        try:
            for record in self._api.records(self._path, self._paginator, self._params):
                self.fetched += 1
                yield record
        except DeelError:
            raise
        except Exception as exc:
            chain = _causes(exc)
            http = next((e for e in chain if isinstance(e, dlt_requests.HTTPError)), None)
            if http is not None:
                status = http.response.status_code if http.response is not None else 0
                if status in (401, 403):
                    raise _auth_error(status) from None
                if status == 429 or status >= 500:
                    self.mark_incomplete(f"http_{status}")
                else:
                    raise DeelError(
                        f"Deel request to {self._path} failed with HTTP {status}."
                    ) from None
            elif any(
                isinstance(e, dlt_requests.RequestException | dlt_requests.RetryError)
                for e in chain
            ):
                self.mark_incomplete("network_error")
            elif any(isinstance(e, ValueError) for e in chain):  # non-JSON body
                self.mark_incomplete("invalid_response")
            else:
                raise
        else:
            if self._paginator.repeated:
                self.mark_incomplete("repeated_page")
            total = self._paginator.total
            if total is not None and self.fetched < total:
                self.mark_incomplete("truncated")


def _causes(exc: BaseException) -> list[BaseException]:
    """The exception and its cause/context chain (dlt wraps errors raised inside resources)."""
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _auth_error(status: int) -> DeelAuthError:
    if status == 401:
        return DeelAuthError(
            "Deel rejected the API token (HTTP 401). Check DEEL_API_TOKEN, that it has not "
            "expired, and that it matches the base URL (sandbox and production tokens are "
            "not interchangeable)."
        )
    return DeelAuthError(
        "Deel denied access (HTTP 403). The token is missing a required scope: "
        "contracts:read for contracts, people:read for people."
    )


# ===========================================================================
# Normalisation: explicit allowlists -> fields (hash input) + document columns
# ===========================================================================


@dataclass
class _Normalised:
    record_id: str
    fields: dict[str, Any]  # allowlisted, scalar-only; the change-detection hash input
    heading: str
    lines: list[tuple[str, Any]]  # (label, value) lines for the Markdown body
    columns: dict[str, Any]  # extra relationship/status columns kept on the row
    updated_at: str | None = None


def _scalar(name: str, value: Any, record_id: str | None = None) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise _BadRecord(f"unexpected_type:{name}", record_id)


def _obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _record_id(record: Any) -> str:
    if not isinstance(record, dict):
        raise _BadRecord("not_an_object")
    raw = record.get("id")
    if raw is None or isinstance(raw, bool | dict | list) or str(raw).strip() == "":
        raise _BadRecord("missing_id")
    return str(raw)


def _normalise_contract(record: Any, cfg: _Config) -> _Normalised:
    rid = _record_id(record)
    client, worker = _obj(record.get("client")), _obj(record.get("worker"))
    sigs = _obj(record.get("signatures"))
    team, entity = _obj(client.get("team")), _obj(client.get("legal_entity"))
    raw = {
        "type": record.get("type"),
        "title": record.get("title"),
        "status": record.get("status"),
        "created_at": record.get("created_at"),
        "termination_date": record.get("termination_date"),
        "is_archived": record.get("is_archived"),
        "is_shielded": record.get("is_shielded"),
        "team_id": team.get("id"),
        "team_name": team.get("name"),
        "legal_entity_id": entity.get("id"),
        "legal_entity_name": entity.get("name"),
        "worker_id": worker.get("id"),
        "client_signed_at": sigs.get("client_signed_at"),
        "worker_signed_at": sigs.get("worker_signed_at"),
        "updated_at": record.get("updated_at"),
    }
    if cfg.include_pii:
        raw["worker_name"] = worker.get("full_name")
        raw["worker_email"] = worker.get("email")
    fields = {"id": rid, **{k: _scalar(k, v, rid) for k, v in raw.items()}}
    labels = [
        ("Type", "type"),
        ("Status", "status"),
        ("Team", "team_name"),
        ("Legal entity", "legal_entity_name"),
        ("Worker id", "worker_id"),
        ("Created", "created_at"),
        ("Client signed", "client_signed_at"),
        ("Worker signed", "worker_signed_at"),
        ("Termination date", "termination_date"),
        ("Archived", "is_archived"),
        ("Shielded", "is_shielded"),
        ("Worker name", "worker_name"),
        ("Worker email", "worker_email"),
    ]
    return _Normalised(
        record_id=rid,
        fields=fields,
        heading=f"Deel contract: {fields['title'] or rid}",
        lines=[("Record", f"contract {_prefixed('contracts', rid)}")]
        + [(label, fields.get(key)) for label, key in labels],
        columns={
            "status": fields["status"],
            "contract_type": fields["type"],
            "worker_id": fields["worker_id"],
            "team_id": fields["team_id"],
            "legal_entity_id": fields["legal_entity_id"],
        },
        updated_at=fields["updated_at"],
    )


def _normalise_person(record: Any, cfg: _Config) -> _Normalised:
    rid = _record_id(record)
    department, manager = _obj(record.get("department")), _obj(record.get("direct_manager"))
    raw = {
        "worker_id": record.get("worker_id"),
        "job_title": record.get("job_title"),
        "seniority": record.get("seniority"),
        "department_id": department.get("id"),
        "department_name": department.get("name"),
        "country": record.get("country"),
        "state": record.get("state"),
        "hiring_type": record.get("hiring_type"),
        "hiring_status": record.get("hiring_status"),
        "start_date": record.get("start_date"),
        "completion_date": record.get("completion_date"),
        "termination_last_day": record.get("termination_last_day"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "direct_manager_id": manager.get("id"),
    }
    if cfg.include_pii:
        emails = record.get("emails")
        raw["full_name"] = record.get("full_name")
        raw["email"] = (
            ", ".join(str(_obj(e).get("value")) for e in emails if _obj(e).get("value"))
            if isinstance(emails, list)
            else None
        ) or None
        addresses = record.get("addresses")
        first = _obj(addresses[0]) if isinstance(addresses, list) and addresses else {}
        raw["address"] = (
            ", ".join(
                str(first[k])
                for k in ("streetAddress", "lineTwo", "locality", "region", "postalCode", "country")
                if first.get(k)
            )
            or None
        )
    fields = {"id": rid, **{k: _scalar(k, v, rid) for k, v in raw.items()}}
    labels = [
        ("Record", None),
        ("Job title", "job_title"),
        ("Seniority", "seniority"),
        ("Department", "department_name"),
        ("Country", "country"),
        ("State", "state"),
        ("Employment type", "hiring_type"),
        ("Status", "hiring_status"),
        ("Start date", "start_date"),
        ("Completion date", "completion_date"),
        ("Last day", "termination_last_day"),
        ("Worker id", "worker_id"),
        ("Manager id", "direct_manager_id"),
        ("Name", "full_name"),
        ("Email", "email"),
        ("Address", "address"),
    ]
    lines: list[tuple[str, Any]] = [
        (label, f"person {_prefixed('people', rid)}" if key is None else fields.get(key))
        for label, key in labels
    ]
    return _Normalised(
        record_id=rid,
        fields=fields,
        heading="Deel worker: " + (fields["job_title"] or fields["worker_id"] or rid),
        lines=lines,
        columns={
            "status": fields["hiring_status"],
            "worker_id": fields["worker_id"],
            "department_id": fields["department_id"],
        },
        updated_at=fields["updated_at"],
    )


def _hash(fields: dict[str, Any]) -> str:
    payload = json.dumps(fields, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:_HASH_LEN]


def _markdown(item: _Normalised) -> str:
    body = [f"- {label}: {value}" for label, value in item.lines if value not in (None, "")]
    return f"# {item.heading}\n\n" + "\n".join(body)


_ID_PREFIX = {"contracts": "deel:contract:", "people": "deel:person:"}


def _prefixed(kind: str, record_id: str) -> str:
    """Deterministic document id so re-syncs update in place."""
    return f"{_ID_PREFIX[kind]}{record_id}"


# ===========================================================================
# Sync: one pass -> changed rows + tombstones + state commit
# ===========================================================================


@dataclass
class _Skips:
    """Skipped records: ids and fixed-vocabulary reasons only, never content."""

    items: list[tuple[str, str | None, str]] = field(default_factory=list)

    def add(self, kind: str, record_id: str | None, reason: str) -> None:
        self.items.append((kind, record_id, reason))
        logger.warning("Deel %s: skipped record %s (%s).", kind, record_id or "<no id>", reason)


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _split_known(entry: str) -> tuple[str, int]:
    digest, _, epoch = entry.partition(":")
    return digest, int(epoch) if epoch.isdigit() else 0


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


@dataclass(frozen=True)
class _Kind:
    name: str
    path: str
    normalise: Any
    paginator: Any


_KINDS = {
    "contracts": _Kind("contracts", "/contracts", _normalise_contract, _cursor_paginator),
    "people": _Kind("people", "/people", _normalise_person, _offset_paginator),
}


def _sync(
    kind_name: str,
    api: _Api,
    cfg: _Config,
    state: dict[str, Any],
    disposition: str,
    skips: _Skips,
    now: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Single pass over one list endpoint.

    Yields changed rows, then (after a clean pass) tombstones. State is only mutated at
    the very end, so an exception anywhere earlier leaves cursor and known ids untouched.
    """
    kind = _KINDS[kind_name]
    now = int(time.time()) if now is None else now
    snapshot = cfg.full_reconcile or disposition != "merge"
    params: dict[str, Any] = {}
    if kind_name == "contracts":
        if cfg.contract_statuses:
            params["statuses"] = list(cfg.contract_statuses)
        if cfg.contract_types:
            params["types"] = list(cfg.contract_types)

    known: dict[str, str] = dict(state.get("known") or {})
    cursor = _parse_ts(state.get("cursor"))
    floor = cursor.timestamp() - cfg.overlap_seconds if cursor else None
    new_known: dict[str, str] = {}
    seen: set[str] = set()
    dropped: set[str] = set()
    max_updated: datetime | None = cursor
    emitted = 0
    skipped_before = len(skips.items)

    sweep = _Pass(api, kind.path, kind.paginator(cfg.page_size), params or None)
    for record in sweep.records():
        try:
            item = kind.normalise(record, cfg)
        except _BadRecord as exc:
            skips.add(kind_name, exc.record_id, exc.reason)
            if exc.record_id is None:
                # An unidentifiable record could be a known one: its absence must not delete.
                sweep.mark_incomplete("record_without_id")
            else:
                seen.add(exc.record_id)
            continue

        rid = item.record_id
        if kind_name == "contracts" and item.columns["status"] in cfg.drop_statuses:
            dropped.add(rid)
            continue
        seen.add(rid)

        digest = _hash(item.fields)
        prev_digest, prev_epoch = _split_known(known[rid]) if rid in known else ("", 0)
        changed = digest != prev_digest
        epoch = now if changed else prev_epoch
        new_known[rid] = f"{digest}:{epoch}"

        updated = _parse_ts(item.updated_at)
        if updated and (max_updated is None or updated > max_updated):
            max_updated = updated
        in_overlap = bool(updated and floor is not None and updated.timestamp() >= floor)

        if snapshot or changed or in_overlap:
            emitted += 1
            yield {
                "id": _prefixed(kind_name, rid),
                "source_id": rid,
                "record_type": {"contracts": "contract", "people": "person"}[kind_name],
                "title": item.heading,
                "content": _markdown(item),
                "updated_at": item.updated_at,
                "last_changed_at": _iso(epoch),
                **item.columns,
                "_deleted": False,
            }

    # --- sweep verdict -----------------------------------------------------------------
    clean = sweep.complete
    if clean and known and sweep.fetched == 0 and not cfg.force_delete:
        # An empty list while records are known looks like scope loss, not mass deletion.
        sweep.mark_incomplete("empty_response")
        clean = False

    tombstones: list[str] = []
    if clean and disposition == "merge":
        tombstones = sorted((set(known) - seen) | (dropped & set(known)))
        ratio = len(tombstones) / len(known) if known else 0.0
        if tombstones and ratio > cfg.max_delete_ratio and not cfg.force_delete:
            raise DeelDeleteGuardError(
                f"Deel {kind_name}: sync would delete {len(tombstones)} of {len(known)} known "
                f"records ({ratio:.0%}), above max_delete_ratio={cfg.max_delete_ratio:.0%}. "
                "Nothing was changed. If this is expected, re-run with force_delete=True."
            )
    for rid in tombstones:
        yield {"id": _prefixed(kind_name, rid), "source_id": rid, "_deleted": True}

    # --- commit state (only reached when the generator ran to completion) -----------------
    if clean:
        state["known"] = new_known
    else:
        # Unseen ids stay known so a later clean pass can still tombstone them.
        state["known"] = {**known, **new_known}
    if max_updated is not None:
        state["cursor"] = max_updated.isoformat()
    state["last_run"] = {
        "at": _iso(now),
        "fetched": sweep.fetched,
        "emitted": emitted,
        "skipped": len(skips.items) - skipped_before,
        "tombstoned": len(tombstones),
        "sweep": "complete" if clean else f"incomplete:{sweep.reason}",
        "mode": "snapshot" if snapshot else "incremental",
    }
    logger.info(
        "Deel %s: fetched=%d emitted=%d skipped=%d tombstoned=%d sweep=%s mode=%s.",
        kind_name,
        sweep.fetched,
        emitted,
        len(skips.items) - skipped_before,
        len(tombstones),
        state["last_run"]["sweep"],
        state["last_run"]["mode"],
    )


# ===========================================================================
# Contract documents (opt-in)
# ===========================================================================


def _extract_text(content_type: str, data: bytes) -> tuple[str | None, str | None]:
    """Returns ``(reason, text)``; reason is set when the file is skipped."""
    if content_type == "application/pdf":
        try:
            import io

            from pypdf import PdfReader
        except ImportError:
            return "pdf_support_missing", None
        try:
            text = "\n".join(
                (page.extract_text() or "") for page in PdfReader(io.BytesIO(data)).pages
            )
        except Exception:  # corrupt/encrypted PDF
            return "unparseable", None
    else:
        text = data.decode("utf-8", errors="replace")
    return (None, text) if text.strip() else ("empty", None)


def _document_rows(
    row: dict[str, Any], api: _Api, cfg: _Config, state: dict[str, Any], skips: _Skips
) -> Iterator[dict[str, Any]]:
    """Per-contract document rows for a contract row emitted by the contracts resource."""
    from dlt.sources.helpers import requests as dlt_requests

    contract_id = str(row["source_id"])
    index: dict[str, list[str]] = state.setdefault("docs", {})

    def doc_row(document_id: str) -> str:
        return f"deel:contract-document:{contract_id}:{document_id}"

    if row.get("_deleted"):
        for document_id in index.pop(contract_id, []):
            yield {"id": doc_row(document_id), "source_id": document_id, "_deleted": True}
        return

    try:
        listing = api.get_json(cfg.documents_path.format(contract_id=contract_id))
    except (dlt_requests.RequestException, ValueError) as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        skips.add("contract_document", contract_id, f"documents_listing_failed:{status or 'error'}")
        return
    items = listing.get("data") if isinstance(listing, dict) else None
    items = items if isinstance(items, list) else []

    listed: list[str] = []
    for position, item in enumerate(items):
        meta = _obj(item)
        document_id = str(meta.get("id") or meta.get("document_type") or f"doc{position}")
        listed.append(document_id)
        # ASSUMPTION: the documented listing is metadata-only; a download link is read from
        # ``download_url``/``url`` if present. Unverified against a live account.
        url = meta.get("download_url") or meta.get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            skips.add("contract_document", f"{contract_id}:{document_id}", "no_download_url")
            continue
        try:
            reason, ctype, data = api.download(url, cfg.document_types, cfg.document_max_bytes)
        except (dlt_requests.RequestException, ValueError):
            reason, ctype, data = "download_failed", None, b""
        text = None
        if reason is None:
            reason, text = _extract_text(ctype or "", data)
        if reason is not None or text is None:
            skips.add("contract_document", f"{contract_id}:{document_id}", reason or "empty")
            continue
        heading = f"Deel contract document ({document_id}) for contract {contract_id}"
        yield {
            "id": doc_row(document_id),
            "source_id": document_id,
            "record_type": "contract_document",
            "contract_id": contract_id,
            "title": heading,
            "content": f"# {heading}\n\n{text}",
            "_deleted": False,
        }

    for stale in set(index.get(contract_id, [])) - set(listed):
        yield {"id": doc_row(stale), "source_id": stale, "_deleted": True}
    index[contract_id] = listed


# ===========================================================================
# Public factory
# ===========================================================================


def _effective_disposition(source_resources: Any, name: str, default: str = "replace") -> str:
    """Write disposition that will actually be applied (the caller's run-level override wins).

    If it cannot be determined, assume ``replace``: that selects the always-safe full snapshot.
    """
    try:
        value = source_resources[name].write_disposition
    except Exception:
        return default
    if isinstance(value, dict):
        value = value.get("disposition")
    return value or default


def _skipped_item(kind: str, record_id: str | None, reason: str, ordinal: int, now: int) -> Any:
    import dlt

    # Unidentifiable records get a per-reason ordinal so they do not collapse into one row.
    discriminator = record_id or f"unknown-{reason}-{ordinal}"
    return dlt.mark.with_table_name(
        {
            "id": f"deel:skipped:{kind}:{discriminator}",
            "record_type": kind,
            "source_id": record_id,
            "reason": reason,
            "title": f"Skipped Deel {kind}",
            "content": f"Skipped Deel {kind} {record_id or '(no id)'}: {reason}",
            "seen_at": _iso(now),
        },
        SKIPPED_TABLE,
    )


def deel_source(
    token: str | None = None,
    base_url: str | None = None,
    resources: Sequence[str] = _ALL_RESOURCES,
    contract_statuses: Sequence[str] | None = None,
    contract_types: Sequence[str] | None = None,
    include_pii: bool = False,
    include_contract_documents: bool = False,
    document_max_bytes: int = 5_000_000,
    document_types: Sequence[str] = DEFAULT_DOCUMENT_TYPES,
    documents_path: str = DEFAULT_DOCUMENTS_PATH,
    page_size: int = MAX_PAGE_SIZE,
    overlap_seconds: int = 3600,
    full_reconcile: bool = False,
    max_delete_ratio: float = 0.5,
    force_delete: bool = False,
    drop_statuses: Sequence[str] | None = None,
    skipped_table: bool = False,
    session: Any = None,
):
    """Create a dlt source that yields Deel contracts / people as Markdown documents.

    Hand it to ``cognee.remember(source, write_disposition="merge", ...)``. ``merge`` is
    what makes sync incremental; if the caller forces ``replace`` the source falls back to a
    full snapshot pass, which is safe but sends every record each run.

    Args:
        token: Deel API token. Falls back to ``DEEL_API_TOKEN``, then dlt secret
            ``sources.deel.api_token``.
        base_url: API base URL. Falls back to ``DEEL_BASE_URL``, then production
            (``DEEL_PRODUCTION_URL``). Use ``DEEL_SANDBOX_URL`` for the sandbox.
        resources: Subset of ``["contracts", "people"]`` (default both).
        contract_statuses / contract_types: Server-side filters for the contracts list.
            Contracts that leave the filter are treated as deleted.
        include_pii: Also ingest worker names/emails/addresses (default off).
        include_contract_documents: Opt in to fetching contract documents (default off;
            the documents endpoint is never called otherwise).
        document_max_bytes / document_types: Size cap and content-type allowlist.
        documents_path: Documents listing path with a ``{contract_id}`` placeholder.
        page_size: Page size, capped at 100 (the documented maximum).
        overlap_seconds: Re-emit records whose ``updated_at`` is within this window of the cursor.
        full_reconcile: Emit every record each run (plus tombstones), as a safety net.
        max_delete_ratio / force_delete: Abort a run that would delete more than this
            fraction of known records, unless ``force_delete`` is set.
        drop_statuses: Contract statuses to remove from the graph (default none:
            terminated/cancelled contracts stay as status updates).
        skipped_table: Also load skipped-record ids/reasons into a ``deel_skipped`` table.
            Off by default because cognee ingests every table of a document source.
        session: Pre-built ``requests`` session (test-injection point).
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Deel connector requires dlt: pip install "dlt[sqlalchemy]" (or cognee[dlt]).'
        ) from exc

    unknown = set(resources) - set(_ALL_RESOURCES)
    if unknown or not resources:
        raise ValueError(f"resources must be a non-empty subset of {list(_ALL_RESOURCES)}.")
    if include_contract_documents and "contracts" not in resources:
        raise ValueError("include_contract_documents requires the 'contracts' resource.")
    if not 0 < max_delete_ratio <= 1:
        raise ValueError("max_delete_ratio must be in (0, 1].")
    if page_size < 1 or overlap_seconds < 0 or document_max_bytes < 1:
        raise ValueError("page_size, document_max_bytes must be >= 1 and overlap_seconds >= 0.")
    if page_size > MAX_PAGE_SIZE:
        logger.warning("Deel: page_size %d capped to %d.", page_size, MAX_PAGE_SIZE)

    cfg = _Config(
        resources=tuple(resources),
        contract_statuses=tuple(contract_statuses or ()),
        contract_types=tuple(contract_types or ()),
        include_pii=include_pii,
        include_contract_documents=include_contract_documents,
        document_max_bytes=document_max_bytes,
        document_types=tuple(t.lower() for t in document_types),
        documents_path=documents_path,
        page_size=min(page_size, MAX_PAGE_SIZE),
        overlap_seconds=overlap_seconds,
        full_reconcile=full_reconcile,
        max_delete_ratio=max_delete_ratio,
        force_delete=force_delete,
        drop_statuses=tuple(drop_statuses or ()),
        skipped_table=skipped_table,
    )
    resolved_token = _resolve_token(token)
    resolved_url = (base_url or os.environ.get(BASE_URL_ENV) or DEEL_PRODUCTION_URL).rstrip("/")

    runtime: dict[str, Any] = {"api": None, "validated": set()}
    skips = _Skips()

    def api_for(kind_name: str) -> _Api:
        """Build the client lazily and validate the token once per endpoint, before any sync."""
        if runtime["api"] is None:
            runtime["api"] = _Api(resolved_url, resolved_token, session or _build_session())
        if kind_name not in runtime["validated"]:
            status = runtime["api"].probe(_KINDS[kind_name].path)
            if status in (401, 403):
                raise _auth_error(status)
            if status >= 400:
                raise DeelError(
                    f"Deel startup check on {_KINDS[kind_name].path} failed with HTTP {status}."
                )
            runtime["validated"].add(kind_name)
        return runtime["api"]

    def flush_skips(now: int):
        if cfg.skipped_table:
            ordinals: dict[tuple[str, str], int] = {}
            for kind, rid, reason in skips.items:
                ordinals[(kind, reason)] = ordinals.get((kind, reason), 0) + 1
                yield _skipped_item(kind, rid, reason, ordinals[(kind, reason)], now)
        skips.items.clear()

    hard_delete = {"_deleted": {"data_type": "bool", "hard_delete": True}}

    def make_resource(kind_name: str, table: str):
        @dlt.resource(name=table, primary_key="id", write_disposition="merge", columns=hard_delete)
        def _resource():
            api = api_for(kind_name)
            disposition = _effective_disposition(dlt.current.source().resources, table)
            now = int(time.time())
            yield from _sync(
                kind_name, api, cfg, dlt.current.resource_state(), disposition, skips, now
            )
            yield from flush_skips(now)

        return _resource

    selected: list[Any] = []
    contracts_resource = None
    if "contracts" in cfg.resources:
        contracts_resource = make_resource("contracts", CONTRACTS_TABLE)()
        selected.append(contracts_resource)
    if "people" in cfg.resources:
        selected.append(make_resource("people", PEOPLE_TABLE)())

    if cfg.include_contract_documents:

        @dlt.transformer(
            data_from=contracts_resource,
            name=DOCUMENTS_TABLE,
            primary_key="id",
            write_disposition="merge",
            columns=hard_delete,
        )
        def _contract_documents(row: dict[str, Any]):
            yield from _document_rows(
                row, api_for("contracts"), cfg, dlt.current.resource_state(), skips
            )
            yield from flush_skips(int(time.time()))

        selected.append(_contract_documents)

    @dlt.source(name=DEEL_SOURCE_NAME)
    def _deel():
        return selected

    source = _deel()
    # Opt into the document ingestion path (row -> text document -> cognify).
    setattr(source, DOCUMENT_SOURCE_ATTR, DEEL_SOURCE_NAME)
    return source
