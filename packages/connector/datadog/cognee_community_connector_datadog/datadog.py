"""Datadog connector for cognee: a dlt resource that syncs the human-written side of Datadog.

It ingests monitors (with their runbook messages), dashboards, notebooks (postmortems
included), SLOs and incidents, never raw metrics, logs or traces, and is meant to be
handed to ``cognee.remember``::

    await cognee.remember(
        datadog_source(
            api_key="<api key>",
            app_key="<application key>",
            site="datadoghq.eu",
            resource_name="datadog_prod",
        ),
        dataset_name="datadog",
        primary_key="id",
        write_disposition="merge",  # required, the add pipeline defaults to "replace"
    )
"""

import hashlib
import json
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import quote

import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from dlt.sources.helpers.requests import Client
from dlt.sources.helpers.rest_client import RESTClient
from dlt.sources.helpers.rest_client.paginators import (
    BasePaginator,
    OffsetPaginator,
    PageNumberPaginator,
)

logger = get_logger("datadog_connector")

# datadog site -> host of its web app; the api lives on api.<site> for every site
SITES = {
    "datadoghq.com": "app.datadoghq.com",
    "us3.datadoghq.com": "us3.datadoghq.com",
    "us5.datadoghq.com": "us5.datadoghq.com",
    "datadoghq.eu": "app.datadoghq.eu",
    "ap1.datadoghq.com": "ap1.datadoghq.com",
    "ap2.datadoghq.com": "ap2.datadoghq.com",
    "ddog-gov.com": "app.ddog-gov.com",
}
# the human-written kinds this source syncs; metrics, logs and traces stay out
KINDS = ("monitors", "dashboards", "notebooks", "slos", "incidents")
# the row id prefix of each kind
_PREFIX = {
    "monitors": "monitor",
    "dashboards": "dashboard",
    "notebooks": "notebook",
    "slos": "slo",
    "incidents": "incident",
}
# the application key scope each kind needs, named in access errors
_SCOPES = {
    "monitors": "monitors_read",
    "dashboards": "dashboards_read",
    "notebooks": "notebooks_read",
    "slos": "slos_read",
    "incidents": "incident_read",
}
# lists don't carry a dashboard's widgets or a notebook's cells, so these two
# are fetched one by one, and only when the list says they changed
_DETAIL_KINDS = ("dashboards", "notebooks")
# the incident list allows at most 100 per page, and 100 is the dashboard and notebook default
_PAGE_SIZE = 100
# a full listing of every kind plus a few hundred changed dashboards or notebooks
DEFAULT_MAX_REQUESTS = 500
# requests left untouched in each endpoint's rate-limit window, for the keys' other users
_REQUESTS_RESERVE = 5
# (connect, read) seconds; our own choice, datadog documents no expected latency
_TIMEOUT = (10, 30)
# longest wait between retries, whatever Retry-After says: cognee holds its dlt staging
# lock for the whole run, so a long sleep would block every other sync
_MAX_RETRY_DELAY = 60


class DatadogSourceError(RuntimeError):
    """Base class of the errors this source raises."""


class DatadogAPIError(DatadogSourceError):
    """Datadog answered with an error, or could not be reached."""


class DatadogAuthError(DatadogSourceError):
    """Datadog rejected the API key (HTTP 401)."""


class DatadogAccessError(DatadogSourceError):
    """The keys cannot read an endpoint (HTTP 403): a wrong key, site or missing scope."""


class DatadogRateLimitedError(DatadogSourceError):
    """Datadog answered HTTP 429."""


class DatadogNotFoundError(DatadogAPIError):
    """The item was deleted or never existed."""


def _listing(kind: str) -> tuple[str, dict[str, Any], str, BasePaginator]:
    """The path, fixed params, data selector and paginator of a kind's list endpoint.

    Every endpoint pages differently, and none filters by modified time.
    """
    if kind == "monitors":
        # without ``page`` the endpoint answers with every monitor at once
        paginator: BasePaginator = PageNumberPaginator(
            base_page=0, page_param="page", total_path=None
        )
        return "/api/v1/monitor", {"page_size": _PAGE_SIZE}, "$", paginator
    if kind == "dashboards":
        paginator = OffsetPaginator(
            limit=_PAGE_SIZE, offset_param="start", limit_param="count", total_path=None
        )
        return "/api/v1/dashboard", {}, "dashboards", paginator
    if kind == "notebooks":
        paginator = OffsetPaginator(
            limit=_PAGE_SIZE,
            offset_param="start",
            limit_param="count",
            total_path="meta.page.total_filtered_count",
        )
        # cells are read per notebook; templates are blank forms, not knowledge
        params = {"include_cells": "false", "is_template": "false"}
        return "/api/v1/notebooks", params, "data", paginator
    if kind == "slos":
        paginator = OffsetPaginator(
            limit=_PAGE_SIZE, offset_param="offset", limit_param="limit", total_path=None
        )
        return "/api/v1/slo", {}, "data", paginator
    paginator = OffsetPaginator(
        limit=_PAGE_SIZE,
        offset_param="page[offset]",
        limit_param="page[size]",
        total_path=None,
    )
    return "/api/v2/incidents", {}, "data", paginator


_ITEM_PATHS = {
    "monitors": "/api/v1/monitor/{}",
    "dashboards": "/api/v1/dashboard/{}",
    "notebooks": "/api/v1/notebooks/{}",
    "slos": "/api/v1/slo/{}",
    "incidents": "/api/v2/incidents/{}",
}


def _check_key(value: str | None, name: str) -> str:
    key = (value or "").strip()
    # reject early and without echoing the key: requests would put it in its exception
    if not key or not key.isascii() or not key.isprintable() or " " in key:
        raise ValueError(f"The Datadog {name} is empty or has an invalid format")
    return key


def _int_header(headers: Any, name: str) -> int | None:
    try:
        return int(headers.get(name))
    except (TypeError, ValueError):
        return None


class DatadogClient:
    """Read-only client for the Datadog endpoints this source uses, built on dlt's RESTClient.

    After every call ``rate_limit`` holds the requests left in that endpoint's window,
    keyed ``"list:<kind>"`` or ``"get:<kind>"``. Datadog limits each endpoint on its own.
    """

    def __init__(
        self,
        api_key: str,
        app_key: str,
        site: str = "datadoghq.com",
        *,
        session: requests.Session | None = None,
    ):
        if site not in SITES:
            raise ValueError(f"Unknown Datadog site {site!r}; use one of {', '.join(SITES)}")
        self.site = site
        if session is None:
            # retry network errors and 5xx only: a 429 stops that kind until the next run
            # instead of sleeping through a window that can last a minute
            session = Client(
                request_timeout=_TIMEOUT,
                request_max_attempts=3,
                request_max_retry_delay=_MAX_RETRY_DELAY,
                status_codes=(500, 502, 503, 504),
                raise_for_status=False,
            ).session
        self._rest = RESTClient(
            base_url=f"https://api.{site}",
            headers={
                "DD-API-KEY": _check_key(api_key, "API key"),
                "DD-APPLICATION-KEY": _check_key(app_key, "application key"),
                "Accept": "application/json",
            },
            session=session,
        )
        self.rate_limit: dict[str, int | None] = {}

    def __repr__(self) -> str:
        return f"DatadogClient(site={self.site!r}, keys=<redacted>)"

    def pages(self, kind: str) -> Iterator[list[dict]]:
        """Yield a kind's list page by page; each page is one request."""
        path, params, selector, paginator = _listing(kind)
        pages = self._rest.paginate(
            path, params=dict(params), paginator=paginator, data_selector=selector
        )
        while True:
            try:
                page = next(pages)
            except StopIteration:
                return
            except requests.HTTPError as exc:
                raise self._error(exc.response, f"list {kind}") from None
            except requests.RequestException as exc:
                raise DatadogAPIError(f"Datadog request failed: {type(exc).__name__}") from None
            self._note_limit(f"list:{kind}", page.response)
            # a page is a list of the selected items; anything else was not a list answer
            if any(not isinstance(item, dict) for item in page):
                raise DatadogAPIError(f"Datadog sent an unreadable {kind} list")
            yield list(page)

    def get(self, kind: str, item_id: str) -> dict:
        """Fetch one item; raises ``DatadogNotFoundError`` once it is deleted."""
        # ids come from datadog or the stored state; quoted so one can't reshape the path
        path = _ITEM_PATHS[kind].format(quote(str(item_id), safe=""))
        try:
            response = self._rest.get(path)
        except requests.RequestException as exc:
            raise DatadogAPIError(f"Datadog request failed: {type(exc).__name__}") from None
        if response.status_code != 200:
            raise self._error(response, f"get one of {kind}")
        self._note_limit(f"get:{kind}", response)
        try:
            payload = response.json()
        except ValueError:
            payload = None
        # v2 and the newer v1 endpoints wrap the item in ``data``
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            payload = payload["data"]
        if not isinstance(payload, dict):
            raise DatadogAPIError(f"Datadog sent an unreadable answer for one of {kind}")
        return payload

    def _note_limit(self, key: str, response: Any) -> None:
        self.rate_limit[key] = _int_header(response.headers, "x-ratelimit-remaining")

    def _error(self, response: Any, action: str) -> DatadogSourceError:
        # the message names the status and the action, never an id or a key
        status = getattr(response, "status_code", None)
        if status == 401:
            return DatadogAuthError("Datadog rejected the API key (HTTP 401)")
        if status == 403:
            return DatadogAccessError(f"Datadog refused to {action} (HTTP 403)")
        if status == 404:
            return DatadogNotFoundError(f"Datadog item not found (HTTP 404) on {action}")
        if status == 429:
            return DatadogRateLimitedError(f"Datadog rate limit hit on {action} (HTTP 429)")
        return DatadogAPIError(f"Datadog request failed on {action}: HTTP {status}")


# ---------------------------------------------------------------------------
# Rendering: deterministic, human-written fields only, nothing volatile.
# ---------------------------------------------------------------------------
def _text(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(sorted(_text(item) for item in value if _text(item)))
    if isinstance(value, bool):
        return "yes" if value else "no"
    return "" if value is None else str(value).strip()


def _lines(fields: list[tuple[str, str]]) -> list[str]:
    return [f"{label}: {value}" for label, value in fields if value]


def _document(row_id: str, title: str, parts: list[str], url: str) -> dict[str, Any]:
    return {
        "id": row_id,
        "title": title,
        "content": "\n".join(parts).strip() or title,
        "url": url,
        "_deleted": False,
    }


def _number(value: Any) -> str:
    # 99.90 and 99.9 are the same target
    return f"{value:g}" if isinstance(value, int | float) else _text(value)


def render_monitor(monitor: dict, app: str) -> dict[str, Any]:
    """A monitor's definition and its message, which is often the runbook.

    ``overall_state`` and the other live fields stay out, so an alert firing does
    not turn the monitor into a new document.
    """
    title = _text(monitor.get("name")) or f"Monitor {monitor['id']}"
    thresholds = (monitor.get("options") or {}).get("thresholds") or {}
    priority = monitor.get("priority")
    fields = [
        ("Type", _text(monitor.get("type"))),
        ("Priority", f"P{priority}" if priority else ""),
        ("Query", _text(monitor.get("query"))),
        (
            "Thresholds",
            ", ".join(
                f"{name} {_number(value)}"
                for name, value in sorted(thresholds.items())
                if value is not None
            ),
        ),
        ("Tags", _text(monitor.get("tags"))),
    ]
    parts = _lines(fields)
    message = _text(monitor.get("message"))
    if message:
        parts.extend(["", "Message:", message])
    return _document(
        f"monitor:{monitor['id']}", title, parts, f"https://{app}/monitors/{monitor['id']}"
    )


def _widget_text(widgets: Any, titles: list[str], notes: list[str]) -> None:
    """Collect widget titles and note text, in layout order, through nested groups."""
    for widget in widgets or []:
        definition = (widget or {}).get("definition") or {}
        kind = definition.get("type")
        if kind == "note":
            if _text(definition.get("content")):
                notes.append(_text(definition.get("content")))
        elif kind == "free_text":
            if _text(definition.get("text")):
                titles.append(_text(definition.get("text")))
        elif _text(definition.get("title")):
            titles.append(_text(definition.get("title")))
        # group and powerpack widgets hold their own widgets
        _widget_text(definition.get("widgets"), titles, notes)


def render_dashboard(dashboard: dict, app: str) -> dict[str, Any]:
    """A dashboard's description, the titles of what it charts, and its note widgets."""
    title = _text(dashboard.get("title")) or f"Dashboard {dashboard['id']}"
    titles: list[str] = []
    notes: list[str] = []
    _widget_text(dashboard.get("widgets"), titles, notes)
    parts = _lines(
        [
            ("Description", _text(dashboard.get("description"))),
            ("Tags", _text(dashboard.get("tags"))),
        ]
    )
    if titles:
        parts.extend(["", "Widgets:", *(f"- {t}" for t in titles)])
    if notes:
        parts.extend(["", "Notes:", *notes])
    path = _text(dashboard.get("url")) or f"/dashboard/{dashboard['id']}"
    return _document(f"dashboard:{dashboard['id']}", title, parts, f"https://{app}{path}")


def render_notebook(notebook: dict, app: str) -> dict[str, Any]:
    """A notebook's markdown cells; a postmortem written as a notebook lands here."""
    attributes = notebook.get("attributes") or {}
    title = _text(attributes.get("name")) or f"Notebook {notebook['id']}"
    metadata = attributes.get("metadata") or {}
    parts = _lines(
        [("Type", _text(metadata.get("type"))), ("Status", _text(attributes.get("status")))]
    )
    for cell in attributes.get("cells") or []:
        definition = ((cell or {}).get("attributes") or {}).get("definition") or {}
        if definition.get("type") == "markdown" and _text(definition.get("text")):
            parts.extend(["", _text(definition.get("text"))])
        elif _text(definition.get("title")):
            # a graph cell: its title says what was looked at
            cell_title = _text(definition.get("title"))
            parts.extend(["", f"[{_text(definition.get('type'))}] {cell_title}"])
    return _document(
        f"notebook:{notebook['id']}", title, parts, f"https://{app}/notebook/{notebook['id']}"
    )


def render_slo(slo: dict, app: str, monitor_names: dict[str, str]) -> dict[str, Any]:
    """An SLO's description, targets and the monitors it is built on."""
    title = _text(slo.get("name")) or f"SLO {slo['id']}"
    targets = []
    for threshold in sorted(slo.get("thresholds") or [], key=lambda t: str(t.get("timeframe"))):
        target = _text(threshold.get("target_display")) or _number(threshold.get("target"))
        warning = _text(threshold.get("warning_display")) or _number(threshold.get("warning"))
        line = f"{target}% over {_text(threshold.get('timeframe'))}"
        targets.append(f"{line} (warning {warning}%)" if warning else line)
    monitors = sorted(
        monitor_names.get(str(monitor_id)) or f"monitor {monitor_id}"
        for monitor_id in slo.get("monitor_ids") or []
    )
    query = slo.get("query") or {}
    fields = [
        ("Type", _text(slo.get("type"))),
        ("Description", _text(slo.get("description"))),
        ("Targets", "; ".join(targets)),
        ("Good events", _text(query.get("numerator"))),
        ("Total events", _text(query.get("denominator"))),
        ("Monitors", ", ".join(monitors)),
        ("Tags", _text(slo.get("tags"))),
    ]
    return _document(
        f"slo:{slo['id']}", title, _lines(fields), f"https://{app}/slo?slo_id={slo['id']}"
    )


def _field_label(name: str) -> str:
    return name.replace("_", " ").strip().capitalize()


def render_incident(incident: dict, app: str) -> dict[str, Any]:
    """An incident's impact and every filled-in field: summary, root cause, services, teams.

    Datadog lets an org add its own incident fields, so all of them are rendered.
    """
    attributes = incident.get("attributes") or {}
    fields = attributes.get("fields") or {}
    values = {
        name: _text((field or {}).get("value"))
        for name, field in fields.items()
        if isinstance(field, dict)
    }
    public_id = attributes.get("public_id")
    name = _text(attributes.get("title")) or "Untitled incident"
    title = f"Incident #{public_id}: {name}" if public_id else name
    impact = _text(attributes.get("customer_impacted"))
    scope = _text(attributes.get("customer_impact_scope"))
    head = [
        # the top-level severity and state are deprecated copies of these fields
        ("Severity", values.pop("severity", "") or _text(attributes.get("severity"))),
        ("State", values.pop("state", "") or _text(attributes.get("state"))),
        ("Customer impact", f"{impact}, {scope}" if impact and scope else impact),
        ("Declared", (_text(attributes.get("declared")) or _text(attributes.get("created")))[:10]),
        ("Resolved", _text(attributes.get("resolved"))[:10]),
    ]
    rest = [(_field_label(key), value) for key, value in sorted(values.items())]
    url = f"https://{app}/incidents/{public_id or incident['id']}"
    return _document(f"incident:{incident['id']}", title, _lines(head + rest), url)


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _is_test(incident: dict) -> bool:
    """Test incidents are drills, not knowledge."""
    return bool((incident.get("attributes") or {}).get("is_test"))


def _stamp(kind: str, item: dict) -> str:
    """The modified time a list gives for an item; used only for the detail kinds."""
    if kind == "notebooks":
        return _text((item.get("attributes") or {}).get("modified"))
    return _text(item.get("modified_at"))


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
class _BudgetSpentError(Exception):
    """Internal: the run's request budget is spent; keep the state reached so far."""


class _WindowSpentError(Exception):
    """Internal: one endpoint's rate-limit window is spent; the other kinds go on."""


class _Walker:
    """One run over the selected kinds, writing progress into ``state``.

    No Datadog list can filter by modified time, so each run lists every kind in
    full and emits a row only when its rendering changed. Ids missing from a
    listing that finished are re-checked one by one, then forgotten.
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
        for key in ("scanned", "skipped", "deferred", "deleted", "failed"):
            stats.setdefault(key, 0)
        self.include = include
        self.max_requests = max_requests
        self.check_active = check_active
        self.requests = 0
        self.app = SITES[client.site]
        # row id -> {"fp": rendering hash, "stamp": list modified time, "name": monitor name}
        self.rows: dict[str, dict] = state.setdefault("rows", {})
        self.refused: list[str] = []

    # -- requests ----------------------------------------------------------
    def _spend(self, limit_key: str) -> None:
        """Count one request, or stop before it when the budget or its window is spent."""
        if self.check_active is not None:
            self.check_active()
        if self.requests >= self.max_requests:
            raise _BudgetSpentError()
        left = (getattr(self.client, "rate_limit", None) or {}).get(limit_key)
        if left is not None and left < _REQUESTS_RESERVE:
            raise _WindowSpentError(limit_key)
        self.requests += 1

    def _list(self, kind: str) -> Iterator[dict]:
        pages = self.client.pages(kind)
        while True:
            self._spend(f"list:{kind}")
            try:
                page = next(pages)
            except StopIteration:
                return
            except DatadogRateLimitedError:
                raise _WindowSpentError(f"list:{kind}") from None
            yield from page

    def _get(self, kind: str, item_id: str) -> dict:
        self._spend(f"get:{kind}")
        try:
            return self.client.get(kind, item_id)
        except DatadogRateLimitedError:
            raise _WindowSpentError(f"get:{kind}") from None

    def _failed(self, step: str, exc: Exception) -> None:
        self.stats["failed"] += 1
        logger.warning("Datadog %s failed for one item, retried next run: %s", step, exc)

    # -- rows --------------------------------------------------------------
    def _changed(self, row: dict) -> bool:
        self.stats["scanned"] += 1
        if self.rows.get(row["id"], {}).get("fp") == _hash(row):
            self.stats["skipped"] += 1
            return False
        return True

    def _emit(self, row: dict, **extra: str) -> dict:
        self.rows[row["id"]] = {"fp": _hash(row), **extra}
        return row

    def _monitor_names(self) -> dict[str, str]:
        return {
            row_id.split(":", 1)[1]: entry["name"]
            for row_id, entry in self.rows.items()
            if row_id.startswith("monitor:") and entry.get("name")
        }

    def _listed_rows(self, kind: str, items: Iterator[dict], seen: set[str]) -> Iterator[dict]:
        """Monitors, SLOs and incidents: the list carries everything that is rendered."""
        monitor_names = self._monitor_names() if kind == "slos" else {}
        for item in items:
            if kind == "incidents" and _is_test(item):
                continue  # left unseen, so one ingested before it was marked a test goes
            if kind == "monitors":
                row = render_monitor(item, self.app)
            elif kind == "slos":
                row = render_slo(item, self.app, monitor_names)
            else:
                row = render_incident(item, self.app)
            seen.add(row["id"])
            extra = {"name": row["title"]} if kind == "monitors" else {}
            if self._changed(row):
                yield self._emit(row, **extra)
            elif extra:
                # a renamed monitor re-renders its row; keep the name the SLOs read current
                self.rows[row["id"]].update(extra)

    def _detail_rows(self, kind: str, items: Iterator[dict], seen: set[str]) -> Iterator[dict]:
        """Dashboards and notebooks: fetch an item only when its list stamp moved."""
        window_spent = False
        for item in items:
            item_id = str(item["id"])
            row_id = f"{_PREFIX[kind]}:{item_id}"
            seen.add(row_id)
            stamp = _stamp(kind, item)
            entry = self.rows.get(row_id)
            if entry is not None and entry.get("stamp") == stamp:
                self.stats["scanned"] += 1
                self.stats["skipped"] += 1
                continue
            if window_spent:
                # the stamp stays old, so the next run fetches it
                self.stats["deferred"] += 1
                continue
            try:
                full = self._get(kind, item_id)
            except _WindowSpentError:
                window_spent = True
                self.stats["deferred"] += 1
                continue
            except DatadogNotFoundError:
                continue  # deleted meanwhile; the next listing leaves it out
            except (DatadogAPIError, DatadogAccessError) as exc:
                self._failed(f"{kind} read", exc)
                continue
            render = render_dashboard if kind == "dashboards" else render_notebook
            row = render(full, self.app)
            if self._changed(row):
                yield self._emit(row, stamp=stamp)
            else:
                # saved without a change that is rendered; remember the new stamp
                self.rows[row_id]["stamp"] = stamp

    # -- forget-on-delete --------------------------------------------------
    def _gone(self, kind: str, item_id: str) -> bool:
        try:
            item = self._get(kind, item_id)
        except DatadogNotFoundError:
            # hard deleted, or a dashboard in the trash, which answers 404 as well;
            # a restored dashboard is back in the list and is read again as new
            return True
        if kind == "notebooks":
            # turned into a template, which this source leaves out
            return bool(((item.get("attributes") or {}).get("metadata") or {}).get("is_template"))
        if kind == "incidents":
            return _is_test(item)
        # still in datadog, so the listing skipped it, e.g. a page shifted under a delete
        return False

    def _forget(self, kind: str, seen: set[str]) -> Iterator[dict]:
        prefix = f"{_PREFIX[kind]}:"
        missing = sorted(r for r in self.rows if r.startswith(prefix) and r not in seen)
        for row_id in missing:
            try:
                gone = self._gone(kind, row_id.split(":", 1)[1])
            except _WindowSpentError:
                return  # still missing next run, so it is checked again then
            except (DatadogAPIError, DatadogAccessError) as exc:
                self._failed("deletion check", exc)
                continue
            if gone:
                del self.rows[row_id]
                self.stats["deleted"] += 1
                yield {"id": row_id, "_deleted": True}

    def _forget_deselected(self) -> Iterator[dict]:
        """Rows of kinds no longer selected leave memory."""
        prefixes = tuple(f"{_PREFIX[k]}:" for k in KINDS if k not in self.include)
        for row_id in sorted(r for r in self.rows if prefixes and r.startswith(prefixes)):
            del self.rows[row_id]
            self.stats["deleted"] += 1
            yield {"id": row_id, "_deleted": True}

    # -- the run -----------------------------------------------------------
    def _kind(self, kind: str) -> Iterator[dict]:
        seen: set[str] = set()
        items = self._list(kind)
        rows = (
            self._detail_rows(kind, items, seen)
            if kind in _DETAIL_KINDS
            else self._listed_rows(kind, items, seen)
        )
        try:
            yield from rows
        except _WindowSpentError:
            # an unfinished listing can't prove anything deleted
            self.stats["failed"] += 1
            self.stats[f"failed_rate_limit_{kind}"] = 1
            logger.warning("Datadog %s rate limit reached; resuming next run.", kind)
            return
        except DatadogAccessError as exc:
            self.refused.append(kind)
            self.stats["failed"] += 1
            self.stats[f"failed_access_{kind}"] = 1
            logger.warning(
                "Datadog refused %s (%s); the application key needs the %s scope.",
                kind,
                exc,
                _SCOPES[kind],
            )
            return
        except DatadogAuthError:
            raise  # the API key itself was rejected; no other kind can succeed
        except DatadogAPIError as exc:
            # a 5xx left after the retries, or an unreadable page: same as a stopped listing
            self.stats["failed"] += 1
            self.stats[f"failed_error_{kind}"] = 1
            logger.warning("Datadog %s listing failed (%s); retried next run.", kind, exc)
            return
        yield from self._forget(kind, seen)

    def rows_out(self) -> Iterator[dict]:
        site = self.state.setdefault("site", self.client.site)
        if site != self.client.site:
            raise DatadogSourceError(
                "This resource_name already holds a sync of another Datadog site; "
                "give each Datadog org its own resource_name"
            )
        yield from self._forget_deselected()
        # monitors go first so SLOs can name the monitors they are built on
        for kind in KINDS:
            if kind in self.include:
                yield from self._kind(kind)
        if self.refused and len(self.refused) == len(self.include):
            raise DatadogAccessError(
                f"Datadog refused every selected kind on {self.client.site}; check that the "
                "keys belong to this site and that the application key has the read scopes"
            )


def _iter_rows(
    client: Any,
    state: dict,
    stats: dict[str, int],
    *,
    include: tuple[str, ...] = KINDS,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict]:
    """Yield the changed rows of an org. Pure of dlt, so tests drive it with a dict."""
    walker = _Walker(
        client,
        state,
        stats,
        include=include,
        max_requests=max_requests,
        check_active=check_active,
    )
    emitted = 0
    try:
        for row in walker.rows_out():
            emitted += 1
            yield row
    except _BudgetSpentError:
        stats["failed"] = max(1, stats.get("failed", 0))
        stats["failed_budget"] = 1
        logger.warning("Datadog sync used its request budget; it resumes on the next run.")
    except DatadogAuthError:
        if not emitted:
            raise
        # a key revoked mid-run: keep the rows already emitted and the state behind them,
        # and let the next run fail loudly on its first request
        stats["failed"] = max(1, stats.get("failed", 0))
        stats["failed_auth"] = 1
        logger.warning("Datadog rejected the API key mid-run; the rows read so far are kept.")


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def datadog_source(
    *,
    resource_name: str,
    api_key: str | None = None,
    app_key: str | None = None,
    site: str = "datadoghq.com",
    service: Any = None,
    check_active: Callable[[], None] | None = None,
    include: tuple[str, ...] = KINDS,
    max_requests: int = DEFAULT_MAX_REQUESTS,
):
    """Return a ``dlt`` resource that yields a Datadog org's documents for ``remember``.

    Args:
        resource_name: Stable name of this connection, one per Datadog org. It scopes
            the sync state and the cleanup of deleted items, so keep it fixed across
            runs. There is no default: two orgs sharing a name would forget each
            other's items, and a run refuses state written for another site.
        api_key: Datadog API key.
        app_key: Datadog application key; a read-only scoped key is enough.
        site: The Datadog site the org lives on, as in ``DD_SITE``: ``datadoghq.com``
            (US1), ``us3.datadoghq.com``, ``us5.datadoghq.com``, ``datadoghq.eu``,
            ``ap1.datadoghq.com``, ``ap2.datadoghq.com`` or ``ddog-gov.com``.
        service: Pre-built client, mainly an injection point for tests.
        check_active: Optional host authorization checkpoint, called before each
            request.
        include: Which of ``"monitors"``, ``"dashboards"``, ``"notebooks"``, ``"slos"``
            and ``"incidents"`` to sync.
        max_requests: Request budget of one run. A bigger org finishes over several
            runs.

    Returns:
        A ``dlt`` resource configured with ``primary_key="id"``,
        ``write_disposition="merge"`` and a ``_deleted`` hard-delete column.
    """
    import dlt

    # version 1 of cognee's document sync scopes cleanup by staging table, which keeps
    # one org's tombstones from touching another table; the later versions add columns
    # this source doesn't write
    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError("Datadog sync requires cognee>=1.6.3 (table-scoped document cleanup).")
    if not isinstance(resource_name, str) or not resource_name.strip():
        raise ValueError("datadog_source needs a resource_name, one per Datadog org")
    if service is None and not (api_key and app_key):
        raise ValueError("datadog_source needs a service, or an api_key and an app_key")
    include = tuple(include)
    if not include or set(include) - set(KINDS):
        raise ValueError(f"include must be a non-empty subset of {KINDS}")

    # built here so a bad key or site fails at construction and the closure holds no raw key
    client = service if service is not None else DatadogClient(api_key or "", app_key or "", site)
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def datadog_org():
        stats.clear()
        stats.update(scanned=0, skipped=0, deferred=0, deleted=0, failed=0)
        # the windows a reused client saw last run have reset since
        client.rate_limit = {}
        rows = _iter_rows(
            client,
            dlt.current.resource_state(),
            stats,
            include=include,
            max_requests=max_requests,
            check_active=check_active,
        )
        yield from dlt_utils.guarded_rows(rows, check_active)

    resource = datadog_org()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "datadog")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    resource.cognee_sync_stats = stats
    return resource
