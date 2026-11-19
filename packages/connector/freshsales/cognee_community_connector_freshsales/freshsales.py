"""DLT source for Freshsales (incremental sync + forget-on-delete).

Pulls Freshsales (Freshworks CRM) **contacts, accounts and deals**, each with its
**notes**, into cognee as documents::

    import cognee
    from cognee_community_connector_freshsales import freshsales_source

    await cognee.remember(
        freshsales_source(domain="acme"),   # FRESHSALES_API_KEY from env
        dataset_name="freshsales",
        write_disposition="merge",          # REQUIRED (see .. important:: below)
    )

Rows are ingested as *normal documents*: the source declares
``cognee_document_source = "freshsales"``, so each record goes through cognify
entity extraction instead of the deterministic dlt-row path.

.. important::
   ``write_disposition="merge"`` is **mandatory**. Incremental runs only see what
   changed, so cognee's default ``"replace"`` would forget everything else.

Design (behaviour checked against a live Freshsales account)
-------------------------------------------------------------
* **Auth** - API key, ``Authorization: Token token=<key>``, against
  ``https://<domain>.myfreshworks.com/crm/sales/api`` (legacy
  ``<domain>.freshsales.io`` hosts are accepted too).
* **Incremental** - ``POST /filtered_search/<entity>`` with the rule
  ``updated_at is_after <cursor>``, sorted by ``updated_at``, 100 per page.
  Live-checked: ``is_after`` is **strict** and timestamps have second precision,
  and an unknown operator is *silently* treated as "equals" instead of failing.
  So the operator is fixed in code and the query starts a safety window
  (5 minutes) before the cursor; ``merge`` makes the overlap harmless. The cursor
  is the UTC time the last run *started*, kept per entity in dlt resource state
  and only advanced after a fully successful run.
* **Full records** - search results are partial (no stage, owner or even
  ``updated_at`` on deals), so each *changed* record is read with one detail call
  that embeds its notes, owner, stage, account and contacts
  (``?include=...``). Unchanged records cost nothing.
* **Allow-listed fields** - documents are built from an explicit list of fields.
  Raw records are never dumped: a live deal record carries chat-widget JWT tokens
  (``fc_widget_collaboration``) that must not reach a knowledge graph. E-mail
  and phone fields are left out by default, and so are derived or volatile values
  (deal age, lead score, open/won totals, assignment times) that change without
  the record being edited.
* **Forget-on-delete** - deleted records go to the entity's *Recycle Bin* view;
  each run reads it and tombstones those ids. ``reconcile=True`` (default) also
  lists all ids of the entity (one request per 100 records) and tombstones known
  ids that are gone, which also covers records purged from the Recycle Bin.
  Tombstones are ``{"id", "_deleted": True}`` rows; dlt removes them on ``merge``
  and cognee's ``orphan_cleanup`` drops them from the graph.
* **Rate limits** - live headers show 5,000 requests/hour and 400/minute
  (``per-min-x-ratelimit-remaining``). The client pauses before the minute budget
  runs out and retries ``429`` / ``5xx`` / network errors; any other error aborts
  the run, so a partial read never moves a cursor or causes a false deletion.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("freshsales_connector")

FRESHSALES_SOURCE_NAME = "freshsales"
RECORDS_TABLE = "freshsales_records"
SAFETY_WINDOW = timedelta(minutes=5)
_PAGE_SIZE = 100
_MAX_RETRIES = 5
_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})
_MIN_BUDGET = 5  # pause when fewer requests than this remain in the minute
_DELETED_COLUMN = {"_deleted": {"data_type": "bool", "hard_delete": True}}

_EXTRA_HINT = (
    "The Freshsales connector requires dlt and httpx: "
    "pip install cognee-community-connector-freshsales"
)


@dataclass(frozen=True)
class _Entity:
    name: str  # user-facing name and row-id prefix
    plural: str  # REST collection and response key
    search: str  # filtered_search entity name
    singular: str  # detail response key
    include: str  # detail ?include=
    view_prefix: str  # name of the "All ..." view
    label: str


ENTITIES = {
    "contacts": _Entity(
        "contacts",
        "contacts",
        "contact",
        "contact",
        "owner,sales_accounts,notes,contact_status,lifecycle_stage",
        "All Contacts",
        "Contact",
    ),
    "accounts": _Entity(
        "accounts",
        "sales_accounts",
        "sales_account",
        "sales_account",
        "owner,notes,industry_type,business_type,contacts",
        "All Accounts",
        "Account",
    ),
    "deals": _Entity(
        "deals",
        "deals",
        "deal",
        "deal",
        "owner,sales_account,contacts,deal_stage,deal_pipeline,deal_type,deal_reason,notes",
        "All Deals",
        "Deal",
    ),
}


@dataclass(frozen=True)
class _Options:
    entities: tuple[_Entity, ...]
    include_notes: bool
    redact_contact_details: bool
    reconcile: bool
    modified_since: str | None


def freshsales_source(
    domain: str | None = None,
    api_key: str | None = None,
    entities: Iterable[str] | None = None,
    include_notes: bool = True,
    redact_contact_details: bool = True,
    reconcile: bool = True,
    modified_since: str | None = None,
    client: Any = None,
):
    """Create a dlt source with Freshsales contacts, accounts and deals.

    Args:
        domain: Account domain: ``"acme"``, ``"acme.myfreshworks.com"`` or a full
            URL. Falls back to ``FRESHSALES_DOMAIN``.
        api_key: Freshsales API key (Settings -> API Settings). Falls back to
            ``FRESHSALES_API_KEY``.
        entities: Any of ``"contacts"``, ``"accounts"``, ``"deals"``. Default: all.
        include_notes: Add each record's notes to its document (default ``True``).
        redact_contact_details: Leave out e-mail and phone fields (default ``True``).
        reconcile: List all ids each run to catch records purged from the
            Recycle Bin (about one request per 100 records).
        modified_since: ISO 8601 time; limits only the *first* run.
        client: ``httpx.Client``-like object (test-injection point) with
            ``get(url, params=, headers=)`` and ``post(url, json=, headers=)``.

    Returns:
        A dlt source for ``cognee.remember(..., write_disposition="merge")``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    domain = domain or os.environ.get("FRESHSALES_DOMAIN")
    api_key = api_key or os.environ.get("FRESHSALES_API_KEY")
    if not (domain and api_key):
        raise ValueError(
            "Freshsales domain and API key required: pass domain= and api_key= "
            "(or set FRESHSALES_DOMAIN / FRESHSALES_API_KEY)."
        )
    names = tuple(dict.fromkeys(entities or ENTITIES))
    unknown = [n for n in names if n not in ENTITIES]
    if unknown:
        raise ValueError(f"Unknown Freshsales entities {unknown}. Use: {sorted(ENTITIES)}.")

    if client is None:
        try:
            import httpx
        except ImportError as exc:
            raise ImportError(_EXTRA_HINT) from exc
        client = httpx.Client(timeout=60.0)

    options = _Options(
        entities=tuple(ENTITIES[n] for n in names),
        include_notes=include_notes,
        redact_contact_details=redact_contact_details,
        reconcile=reconcile,
        modified_since=modified_since,
    )
    api = _Api(client, base_url(domain), api_key)

    @dlt.resource(
        name=RECORDS_TABLE, primary_key="id", write_disposition="merge", columns=_DELETED_COLUMN
    )
    def freshsales_records():
        yield from _sync(api, dlt.current.resource_state(), options)

    @dlt.source(name=FRESHSALES_SOURCE_NAME)
    def _freshsales():
        return freshsales_records

    source = _freshsales()
    # Opt into the document ingestion path (row -> text document -> cognify).
    setattr(source, DOCUMENT_SOURCE_ATTR, FRESHSALES_SOURCE_NAME)
    return source


def base_url(domain: str) -> str:
    """API base URL from ``acme``, ``acme.myfreshworks.com`` or a full URL."""
    host = domain.strip().removeprefix("https://").removeprefix("http://").split("/")[0]
    if not host:
        raise ValueError("Empty Freshsales domain.")
    if "." not in host:
        host = f"{host}.myfreshworks.com"
    if host.endswith(".freshsales.io"):
        return f"https://{host}/api"
    return f"https://{host}/crm/sales/api"


# ---------------------------------------------------------------------------
# HTTP: auth, pacing, retries
# ---------------------------------------------------------------------------


class _Api:
    def __init__(self, http: Any, base: str, api_key: str):
        self._http = http
        self._base = base
        self._headers = {
            "Authorization": f"Token token={api_key}",
            "Content-Type": "application/json",
        }
        self.requests = 0

    def get(self, path: str, params: dict | None = None) -> dict:
        return self._send(
            lambda: self._http.get(f"{self._base}/{path}", params=params, headers=self._headers),
            path,
        )

    def post(self, path: str, body: dict) -> dict:
        return self._send(
            lambda: self._http.post(f"{self._base}/{path}", json=body, headers=self._headers),
            path,
        )

    def _send(self, send, path: str) -> dict:
        response = _with_retries(send)
        self.requests += 1
        _pace(response.headers)
        status = response.status_code
        if status in (401, 403):
            raise PermissionError(
                f"Freshsales refused {path} (HTTP {status}). "
                "Check FRESHSALES_API_KEY and the domain."
            )
        if status >= 400:
            raise RuntimeError(f"Freshsales error on {path} (HTTP {status}): {_body(response)}")
        return response.json()


def _body(response: Any) -> str:
    try:
        return str(response.json())[:300]
    except Exception:
        return ""


def _with_retries(send) -> Any:
    import httpx

    for attempt in range(_MAX_RETRIES):
        try:
            response = send()
        except httpx.TransportError as exc:
            if attempt == _MAX_RETRIES - 1:
                raise
            delay = float(2**attempt)
            logger.warning("Freshsales: %s - retrying in %.1fs.", exc, delay)
            time.sleep(delay)
            continue
        if response.status_code in _TRANSIENT_STATUS and attempt < _MAX_RETRIES - 1:
            delay = _retry_after(response.headers, attempt)
            logger.warning(
                "Freshsales: HTTP %s - retrying in %.1fs (%d/%d).",
                response.status_code,
                delay,
                attempt + 1,
                _MAX_RETRIES,
            )
            time.sleep(delay)
            continue
        return response
    raise RuntimeError("Freshsales: retry loop exited unexpectedly.")  # pragma: no cover


def _retry_after(headers: Any, attempt: int) -> float:
    value = headers.get("Retry-After") if headers else None
    try:
        return max(float(value), 0.0)
    except (TypeError, ValueError):
        # Rate limits are per minute; back off long enough for a new window.
        return 60.0 if attempt else 10.0


def _pace(headers: Any) -> None:
    """Pause for the next minute window when the minute budget is almost gone."""
    value = headers.get("per-min-x-ratelimit-remaining") if headers else None
    try:
        remaining = int(value)
    except (TypeError, ValueError):
        return
    if remaining < _MIN_BUDGET:
        logger.info("Freshsales: %d request(s) left this minute - pausing 60s.", remaining)
        time.sleep(60.0)


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _sync(api: _Api, state: dict, options: _Options) -> Iterator[dict]:
    cursors: dict = state.setdefault("cursors", {})
    known: dict = state.setdefault("known_ids", {})
    run_started = _utc_now()
    new_cursors: dict[str, str] = {}
    views: dict[str, dict[str, int]] = {}
    totals = {"synced": 0, "forgotten": 0}

    for entity in options.entities:
        ids = set(known.get(entity.name) or [])
        cursor = cursors.get(entity.name)
        since = _since(cursor, options.modified_since)

        changed: set[str] = set()
        for record_id in _changed_ids(api, entity, since):
            if record_id in changed:
                continue
            detail = api.get(
                f"{entity.plural}/{record_id}", {"include": _includes(entity, options)}
            )
            record = detail.get(entity.singular) or {}
            if not record or record.get("is_deleted"):
                continue  # handled by the Recycle Bin pass below
            changed.add(record_id)
            ids.add(record_id)
            totals["synced"] += 1
            yield _record_row(entity, record, detail, options)

        gone: set[str] = set()
        if cursor:  # nothing can be forgotten before the first load
            entity_views = views.get(entity.name) or _views(api, entity)
            views[entity.name] = entity_views
            recycle = entity_views.get("Recycle Bin")
            if recycle:
                gone.update(_view_ids(api, entity, recycle))
            if options.reconcile:
                everyone = entity_views.get(entity.view_prefix)
                if everyone is None:
                    raise RuntimeError(f"Freshsales: no '{entity.view_prefix}' view to reconcile.")
                present = set(_view_ids(api, entity, everyone))
                gone.update(ids - present - changed)
            gone &= ids  # only forget what this source loaded
            gone -= changed  # seen alive in this run
        for record_id in sorted(gone):
            totals["forgotten"] += 1
            yield {"id": f"{entity.name}:{record_id}", "_deleted": True}

        known[entity.name] = sorted(ids - gone)
        new_cursors[entity.name] = _iso(run_started)

    cursors.update(new_cursors)  # reached only when every entity finished
    logger.info(
        "Freshsales: %d record(s) synced, %d forgotten, %d request(s).",
        totals["synced"],
        totals["forgotten"],
        api.requests,
    )


def _since(cursor: str | None, modified_since: str | None) -> str | None:
    value = cursor or modified_since
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Invalid modified_since {value!r}; use ISO 8601.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    # is_after is strict with second precision: start a safety window earlier.
    return _iso((parsed - SAFETY_WINDOW).astimezone(UTC))


def _includes(entity: _Entity, options: _Options) -> str:
    parts = entity.include.split(",")
    if not options.include_notes:
        parts = [p for p in parts if p != "notes"]
    return ",".join(parts)


def _changed_ids(api: _Api, entity: _Entity, since: str | None) -> Iterator[str]:
    """Ids changed after ``since`` (all ids when ``since`` is None)."""
    value = since or "1970-01-01T00:00:00Z"
    rule = [{"attribute": "updated_at", "operator": "is_after", "value": value}]
    page = 1
    while True:
        payload = api.post(
            f"filtered_search/{entity.search}",
            {
                "filter_rule": rule,
                "per_page": _PAGE_SIZE,
                "page": page,
                "sort": "updated_at",
                "sort_type": "asc",
            },
        )
        items = payload.get(entity.plural) or []
        for item in items:
            if item.get("id") is not None:
                yield str(item["id"])
        if len(items) < _PAGE_SIZE:
            return
        page += 1


def _views(api: _Api, entity: _Entity) -> dict[str, int]:
    payload = api.get(f"{entity.plural}/filters")
    return {v["name"]: v["id"] for v in payload.get("filters") or [] if v.get("name")}


def _view_ids(api: _Api, entity: _Entity, view_id: int) -> Iterator[str]:
    page = 1
    while True:
        payload = api.get(f"{entity.plural}/view/{view_id}", {"per_page": _PAGE_SIZE, "page": page})
        items = payload.get(entity.plural) or []
        for item in items:
            if item.get("id") is not None:
                yield str(item["id"])
        total_pages = int((payload.get("meta") or {}).get("total_pages") or 1)
        if not items or page >= total_pages:
            return
        page += 1


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


def _record_row(entity: _Entity, record: dict, detail: dict, options: _Options) -> dict:
    lookup = _Lookup(detail)
    if entity.name == "contacts":
        title = record.get("display_name") or " ".join(
            p for p in (record.get("first_name"), record.get("last_name")) if p
        )
        lines = [
            ("Job title", record.get("job_title")),
            ("Status", lookup.name("contact_status", record.get("contact_status_id"))),
            ("Lifecycle stage", lookup.name("lifecycle_stages", record.get("lifecycle_stage_id"))),
            ("Accounts", lookup.names("sales_accounts", _account_ids(record))),
            ("Owner", lookup.name("users", record.get("owner_id"), "display_name")),
            ("Location", _place(record)),
            ("Tags", ", ".join(record.get("tags") or [])),
            ("Created", record.get("created_at")),
            ("Description", record.get("description")),
        ]
        if not options.redact_contact_details:
            lines[1:1] = [
                ("Email", record.get("email")),
                ("Work phone", record.get("work_number")),
                ("Mobile", record.get("mobile_number")),
            ]
    elif entity.name == "accounts":
        title = record.get("name")
        lines = [
            ("Industry", lookup.name("industry_types", record.get("industry_type_id"))),
            ("Business type", lookup.name("business_types", record.get("business_type_id"))),
            ("Website", record.get("website")),
            ("Employees", record.get("number_of_employees")),
            ("Annual revenue", record.get("annual_revenue")),
            ("Contacts", lookup.names("contacts", record.get("contact_ids"), "display_name")),
            ("Owner", lookup.name("users", record.get("owner_id"), "display_name")),
            ("Location", _place(record)),
            ("Tags", ", ".join(record.get("tags") or [])),
            ("Created", record.get("created_at")),
            ("Description", record.get("description")),
        ]
        if not options.redact_contact_details:
            lines.insert(3, ("Phone", record.get("phone")))
    else:
        title = record.get("name")
        lines = [
            ("Amount", record.get("amount")),
            ("Stage", lookup.name("deal_stages", record.get("deal_stage_id"))),
            ("Pipeline", lookup.name("deal_pipelines", record.get("deal_pipeline_id"))),
            (
                "Probability",
                f"{record['probability']}%" if record.get("probability") is not None else None,
            ),
            ("Expected close", record.get("expected_close")),
            ("Closed", record.get("closed_date")),
            ("Type", lookup.name("deal_types", record.get("deal_type_id"))),
            ("Lost reason", lookup.name("deal_reasons", record.get("deal_reason_id"))),
            ("Account", lookup.name("sales_accounts", record.get("sales_account_id"))),
            ("Contacts", lookup.names("contacts", record.get("contact_ids"), "display_name")),
            ("Owner", lookup.name("users", record.get("owner_id"), "display_name")),
            ("Tags", ", ".join(record.get("tags") or [])),
            ("Created", record.get("created_at")),
        ]

    body = [f"Type: {entity.label}"]
    body += [f"{label}: {_text(value)}" for label, value in lines if _text(value)]
    notes = _notes(detail) if options.include_notes else []
    if notes:
        body.append("Notes:\n" + "\n".join(notes))
    return {
        "id": f"{entity.name}:{record['id']}",
        "title": _text(title) or f"{entity.label} {record['id']}",
        "content": "\n".join(body),
        "_deleted": False,
    }


class _Lookup:
    """Resolve ids to names using the objects embedded by ``?include=``."""

    def __init__(self, detail: dict):
        self._by_kind: dict[str, dict[str, dict]] = {}
        for kind, items in detail.items():
            if isinstance(items, list):
                self._by_kind[kind] = {str(i.get("id")): i for i in items if isinstance(i, dict)}

    def name(self, kind: str, item_id: Any, key: str = "name") -> str:
        item = self._by_kind.get(kind, {}).get(str(item_id))
        return _text(item.get(key) or item.get("name")) if item else ""

    def names(self, kind: str, ids: Any, key: str = "name") -> str:
        found = [self.name(kind, i, key) for i in ids or []]
        return ", ".join(n for n in found if n)


def _account_ids(record: dict) -> list:
    accounts = record.get("sales_accounts") or []
    ids = [a.get("id") for a in accounts if isinstance(a, dict)]
    return ids or record.get("sales_account_ids") or []


def _place(record: dict) -> str:
    return ", ".join(_text(record.get(k)) for k in ("city", "state", "country") if record.get(k))


def _notes(detail: dict) -> list[str]:
    users = {str(u.get("id")): u.get("display_name") for u in detail.get("users") or []}
    lines = []
    for note in sorted(
        detail.get("notes") or [], key=lambda n: (n.get("created_at") or "", n.get("id") or 0)
    ):
        text = _html_to_text(note.get("description"))
        if not text:
            continue
        author = users.get(str(note.get("creater_id") or note.get("creator_id"))) or ""
        when = note.get("created_at") or ""
        prefix = " ".join(p for p in (when, f"by {author}" if author else "") if p)
        lines.append(f"- [{prefix}] {text}" if prefix else f"- {text}")
    return lines


def _text(value: Any) -> str:
    if value is None or value == "":
        return ""
    return " ".join(str(value).split())


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("br", "p", "div", "li"):
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _html_to_text(value: Any) -> str:
    if not value:
        return ""
    parser = _TextExtractor()
    parser.feed(str(value))
    parser.close()
    return " ".join("".join(parser.parts).split())
