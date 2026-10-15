"""Google Analytics 4 connector for cognee: a dlt resource that syncs a property's setup to memory.

It ingests custom dimensions, custom metrics, key events, audience definitions and
reporting annotations, never report data, and is meant to be handed to ``cognee.remember``::

    await cognee.remember(
        ga4_source("123456789", credentials_path="service-account.json"),
        dataset_name="ga4",
        primary_key="id",
        write_disposition="merge",  # required, the add pipeline defaults to "replace"
    )
"""

import hashlib
import json
import logging
import time
from collections.abc import Callable, Iterator
from typing import Any

import google.auth
import httplib2
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from google.auth.exceptions import GoogleAuthError, RefreshError, TransportError
from google.oauth2 import service_account
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

# read-only on purpose: the change history api would need analytics.edit
SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]
KINDS = ("custom_dimensions", "custom_metrics", "key_events", "audiences", "annotations")
# admin api version and collection of each kind; audiences and annotations exist only in v1alpha
_COLLECTIONS = {
    "custom_dimensions": ("v1beta", "customDimensions"),
    "custom_metrics": ("v1beta", "customMetrics"),
    "key_events": ("v1beta", "keyEvents"),
    "audiences": ("v1alpha", "audiences"),
    "annotations": ("v1alpha", "reportingDataAnnotations"),
}
_PAGE_SIZE = 200
_TIMEOUT = 30
_RETRIES = 3


class GA4SourceError(RuntimeError):
    """Base class of the errors this source raises."""


class GA4APIError(GA4SourceError):
    """Google Analytics answered with an error or an unexpected shape, or could not be reached."""


class GA4AuthError(GA4SourceError):
    """Google rejected the credentials, or none were found."""


class GA4AccessError(GA4SourceError):
    """The credentials cannot read the property, or the Admin API is not enabled (HTTP 403)."""


class GA4RateLimitedError(GA4SourceError):
    """The Admin API quota is exhausted (HTTP 429)."""


def load_credentials(credentials_path: str | None = None) -> Any:
    """Read-only credentials from a service-account key file, else the application defaults."""
    if credentials_path:
        return service_account.Credentials.from_service_account_file(
            credentials_path, scopes=SCOPES
        )
    try:
        credentials, _ = google.auth.default(scopes=SCOPES)
    except GoogleAuthError:
        raise GA4AuthError(
            "No Google credentials found: pass credentials_path or set "
            "GOOGLE_APPLICATION_CREDENTIALS to a service-account key file"
        ) from None
    return credentials


def build_admin_services(credentials: Any) -> dict[str, Any]:
    """Admin API clients for v1beta and v1alpha, built from the bundled discovery documents."""
    return {
        version: build(
            "analyticsadmin",
            version,
            http=AuthorizedHttp(credentials, http=httplib2.Http(timeout=_TIMEOUT)),
            cache_discovery=False,
            static_discovery=True,
        )
        for version in ("v1beta", "v1alpha")
    }


def _google_error(exc: HttpError) -> tuple[str, set[str]]:
    """The status and reasons of a Google error body. Only matched, never echoed."""
    try:
        error = json.loads(exc.content).get("error") or {}
        details = error.get("details") or []
        reasons = {detail.get("reason") for detail in details if isinstance(detail, dict)}
        status = error.get("status")
    except (AttributeError, TypeError, ValueError):
        return "", set()
    return (status if isinstance(status, str) else ""), {r for r in reasons if isinstance(r, str)}


def _error(exc: HttpError, collection: str) -> GA4SourceError:
    status = exc.resp.status
    google_status, reasons = _google_error(exc)
    if status == 401:
        return GA4AuthError("Google rejected the credentials (HTTP 401)")
    if status == 403 and "SERVICE_DISABLED" in reasons:
        return GA4AccessError(
            "The Google Analytics Admin API is not enabled in the service account's "
            "Google Cloud project (HTTP 403)"
        )
    if status == 403:
        return GA4AccessError(
            "The credentials cannot read this property; add the service account's email "
            "to the property as a Viewer (HTTP 403)"
        )
    if status == 429:
        return GA4RateLimitedError("Google Analytics Admin API quota exhausted (HTTP 429)")
    return GA4APIError(f"Google Analytics answered {collection} with HTTP {status} {google_status}")


class GA4Client:
    """Minimal read-only client for the Admin API listings this source needs.

    Tests can pass any object with the same ``list`` method instead.
    """

    def __init__(self, services: dict[str, Any], *, sleep: Callable[[float], None] = time.sleep):
        self._services = services
        self._sleep = sleep

    def __repr__(self) -> str:
        return "GA4Client(credentials=<redacted>)"

    def list(self, kind: str, property_id: str) -> list[dict[str, Any]]:
        """Every record of a kind on a property, following the page tokens to the end."""
        version, collection = _COLLECTIONS[kind]
        listing = getattr(self._services[version].properties(), collection)()
        params: dict[str, Any] = {"parent": f"properties/{property_id}", "pageSize": _PAGE_SIZE}
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        while True:
            page = self._execute(listing.list(**params), collection)
            items = page.get(collection, []) if isinstance(page, dict) else None
            if not isinstance(items, list):
                raise GA4APIError(f"Google Analytics answered {collection} in an unexpected shape")
            records.extend(items)
            token = page.get("nextPageToken")
            if not token:
                return records
            # a token that repeats would page forever and could hide records
            if token in seen:
                raise GA4APIError(f"Google Analytics paging of {collection} did not advance")
            seen.add(token)
            params["pageToken"] = token

    def _execute(self, request: Any, collection: str) -> Any:
        for attempt in range(_RETRIES):
            last = attempt == _RETRIES - 1
            try:
                return request.execute(num_retries=0)
            except HttpError as exc:
                if exc.resp.status >= 500 and not last:
                    self._sleep(2**attempt)
                    continue
                raise _error(exc, collection) from None
            except RefreshError:
                raise GA4AuthError(
                    "Google rejected the service-account key; check it is valid and not revoked"
                ) from None
            except (TransportError, httplib2.HttpLib2Error, OSError) as exc:
                if last:
                    raise GA4APIError(
                        f"Google Analytics request failed: {type(exc).__name__}"
                    ) from None
                self._sleep(2**attempt)
        raise GA4APIError("Google Analytics request failed")  # pragma: no cover


# ---------------------------------------------------------------------------
# Rendering: deterministic, definitions only, nothing volatile.
# ---------------------------------------------------------------------------
def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _label(value: Any) -> str:
    """An API enum in words, e.g. ``ONCE_PER_SESSION`` becomes ``once per session``."""
    text = _text(value)
    return "" if not text or text.endswith("UNSPECIFIED") else text.lower().replace("_", " ")


def _lines(fields: list[tuple[str, str]]) -> list[str]:
    return [f"{label}: {value}" for label, value in fields if value]


def _document(row_id: str, title: str, fields: list[tuple[str, str]], *sections: list[str]) -> dict:
    parts = _lines(fields)
    for section in sections:
        if section:
            parts.extend(["", *section])
    return {
        "id": row_id,
        "title": title,
        "content": "\n".join(parts).strip() or title,
        "url": "",
        "_deleted": False,
    }


def render_custom_dimension(dimension: dict[str, Any]) -> dict[str, Any]:
    fields = [
        ("Custom dimension", _text(dimension.get("parameterName"))),
        ("Scope", _label(dimension.get("scope"))),
        ("Ads personalization", "excluded" if dimension.get("disallowAdsPersonalization") else ""),
    ]
    return _document(
        dimension["name"],
        _text(dimension.get("displayName")) or _text(dimension.get("parameterName")),
        fields,
        [_text(dimension.get("description"))],
    )


def render_custom_metric(metric: dict[str, Any]) -> dict[str, Any]:
    restricted_types = metric.get("restrictedMetricType") or []
    restricted = ", ".join(sorted(_label(kind) for kind in restricted_types))
    fields = [
        ("Custom metric", _text(metric.get("parameterName"))),
        ("Scope", _label(metric.get("scope"))),
        ("Unit", _label(metric.get("measurementUnit"))),
        ("Restricted data", restricted),
    ]
    return _document(
        metric["name"],
        _text(metric.get("displayName")) or _text(metric.get("parameterName")),
        fields,
        [_text(metric.get("description"))],
    )


def render_key_event(event: dict[str, Any]) -> dict[str, Any]:
    default = event.get("defaultValue") or {}
    value = f"{_text(default.get('numericValue'))} {_text(default.get('currencyCode'))}".strip()
    fields = [
        ("Key event", _text(event.get("eventName"))),
        ("Counted", _label(event.get("countingMethod"))),
        ("Default value", value),
        ("Event", "custom" if event.get("custom") else "Google Analytics default"),
    ]
    return _document(event["name"], _text(event.get("eventName")), fields)


_OPERATORS = {"EQUAL": "=", "LESS_THAN": "<", "GREATER_THAN": ">"}
_MATCHES = {
    "EXACT": "is",
    "BEGINS_WITH": "begins with",
    "ENDS_WITH": "ends with",
    "CONTAINS": "contains",
    "FULL_REGEXP": "matches the regex",
}


def _number(value: Any) -> str:
    value = value if isinstance(value, dict) else {}
    return _text(value.get("int64Value", value.get("doubleValue")))


def _condition(spec: dict[str, Any]) -> str:
    """One dimension or metric test, e.g. ``event_count > 1 in any 7-day period``."""
    if "stringFilter" in spec:
        match = spec["stringFilter"]
        kind = match.get("matchType")
        test = f"{_MATCHES.get(kind, _label(kind))} '{_text(match.get('value'))}'"
        test += " (case sensitive)" if match.get("caseSensitive") else ""
    elif "inListFilter" in spec:
        values = sorted(_text(value) for value in spec["inListFilter"].get("values") or [])
        test = f"is one of {', '.join(values)}"
    elif "numericFilter" in spec:
        match = spec["numericFilter"]
        kind = match.get("operation")
        test = f"{_OPERATORS.get(kind, _label(kind))} {_number(match.get('value'))}"
    elif "betweenFilter" in spec:
        match = spec["betweenFilter"]
        test = f"between {_number(match.get('fromValue'))} and {_number(match.get('toValue'))}"
    else:
        return json.dumps(spec, sort_keys=True)
    days = _text(spec.get("inAnyNDayPeriod"))
    window = f" in any {days}-day period" if days else ""
    ever = " at any point in time" if spec.get("atAnyPointInTime") else ""
    return f"{_text(spec.get('fieldName'))} {test}{window}{ever}"


def _expression(expression: Any) -> str:
    """An audience filter expression as one line; and/or groups are sorted, they commute."""
    if not isinstance(expression, dict):
        return ""
    for key, joiner in (("andGroup", " and "), ("orGroup", " or ")):
        if key in expression:
            group = expression[key].get("filterExpressions") or []
            parts = sorted(part for part in map(_expression, group) if part)
            return parts[0] if len(parts) == 1 else f"({joiner.join(parts)})"
    if "notExpression" in expression:
        return f"not {_expression(expression['notExpression'])}"
    if "eventFilter" in expression:
        event = expression["eventFilter"]
        where = _expression(event.get("eventParameterFilterExpression"))
        name = f"event {_text(event.get('eventName'))}"
        return f"{name} where {where}" if where else name
    if "dimensionOrMetricFilter" in expression:
        return _condition(expression["dimensionOrMetricFilter"])
    # an expression kind this version does not know is kept as data, not dropped
    return json.dumps(expression, sort_keys=True) if expression else ""


def _scope(value: Any) -> str:
    return _label(_text(value).removeprefix("AUDIENCE_FILTER_SCOPE_")) or "any scope"


def _clause(clause: dict[str, Any]) -> str:
    verb = "Exclude" if clause.get("clauseType") == "EXCLUDE" else "Include"
    if "simpleFilter" in clause:
        match = clause["simpleFilter"]
        rule = _expression(match.get("filterExpression"))
        return f"{verb} users ({_scope(match.get('scope'))}): {rule}"
    if "sequenceFilter" in clause:
        match = clause["sequenceFilter"]
        steps = []
        for step in match.get("sequenceSteps") or []:
            step_text = _expression(step.get("filterExpression"))
            if steps:
                step_text = (
                    "directly then " if step.get("immediatelyFollows") else "then "
                ) + step_text
            if step.get("constraintDuration"):
                step_text += f" within {_text(step['constraintDuration'])}"
            steps.append(step_text)
        limit = _text(match.get("sequenceMaximumDuration"))
        rule = ", ".join(steps) + (f", all within {limit}" if limit else "")
        return f"{verb} users who in order ({_scope(match.get('scope'))}): {rule}"
    return json.dumps(clause, sort_keys=True)


def render_audience(audience: dict[str, Any]) -> dict[str, Any]:
    clauses = [clause for clause in audience.get("filterClauses") or [] if isinstance(clause, dict)]
    excludes = any(clause.get("clauseType") == "EXCLUDE" for clause in clauses)
    trigger = audience.get("eventTrigger") or {}
    joined = _text(trigger.get("eventName"))
    days = _text(audience.get("membershipDurationDays"))
    fields = [
        ("Membership duration", f"{days} days" if days else ""),
        ("Exclusion", _label(audience.get("exclusionDurationMode")) if excludes else ""),
        ("Logs event", f"{joined} ({_label(trigger.get('logCondition'))})" if joined else ""),
    ]
    # the api calls the clauses unordered, so sort them to keep the fingerprint stable
    rules = sorted(_clause(clause) for clause in clauses)
    return _document(
        audience["name"],
        _text(audience.get("displayName")) or "Untitled audience",
        fields,
        [_text(audience.get("description"))],
        # default audiences come back without filter clauses
        ["Definition:", *rules] if rules else [],
    )


def _date(value: Any) -> str:
    parts = [value.get(key) for key in ("year", "month", "day")] if isinstance(value, dict) else []
    if len(parts) != 3 or not all(isinstance(part, int) for part in parts):
        return ""
    return f"{parts[0]:04d}-{parts[1]:02d}-{parts[2]:02d}"


def render_annotation(annotation: dict[str, Any]) -> dict[str, Any]:
    span = annotation.get("annotationDateRange") or {}
    start, end = _date(span.get("startDate")), _date(span.get("endDate"))
    when = f"{start} to {end}" if start and end else _date(annotation.get("annotationDate"))
    fields = [
        ("Annotation date", when),
        ("Added by", "Google Analytics" if annotation.get("systemGenerated") else ""),
    ]
    return _document(
        annotation["name"],
        _text(annotation.get("title")) or "Untitled annotation",
        fields,
        [_text(annotation.get("description"))],
    )


_RENDERERS = {
    "custom_dimensions": render_custom_dimension,
    "custom_metrics": render_custom_metric,
    "key_events": render_key_event,
    "audiences": render_audience,
    "annotations": render_annotation,
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


class _Walker:
    """One run over the selected kinds of a property, writing progress into ``state``.

    The Admin API has no change feed and these resources carry no update time, so
    every run lists each kind in full and emits a row only when its fingerprint
    changed. An id that a complete listing no longer holds is forgotten.
    """

    def __init__(
        self,
        client: Any,
        property_id: str,
        state: dict,
        stats: dict[str, int],
        *,
        include: tuple[str, ...],
    ):
        self.client = client
        self.property_id = property_id
        self.stats = stats
        for key in ("scanned", "skipped", "deleted", "failed"):
            stats.setdefault(key, 0)
        self.include = include
        self.emitted = 0
        self.fingerprints: dict[str, str] = state.setdefault("fingerprints", {})

    def _prefix(self, kind: str) -> str:
        return f"properties/{self.property_id}/{_COLLECTIONS[kind][1]}/"

    def _listing(self, kind: str) -> list[dict]:
        """Return a kind's records, refusing any shape a delete could not be trusted on."""
        try:
            records = self.client.list(kind, self.property_id)
        except GA4RateLimitedError:
            raise _CutShortError("rate_limit") from None
        except (GA4AuthError, GA4AccessError):
            if self.emitted:
                raise _CutShortError("access") from None
            raise
        prefix = self._prefix(kind)
        complete = isinstance(records, list) and all(
            isinstance(record, dict)
            and _text(record.get("name")).startswith(prefix)
            and len(_text(record.get("name"))) > len(prefix)
            for record in records
        )
        if not complete:
            raise GA4APIError(f"Google Analytics answered {kind} with an unexpected shape")
        return records

    def _changed(self, row: dict) -> Iterator[dict]:
        self.stats["scanned"] += 1
        fingerprint = _hash(row)
        if self.fingerprints.get(row["id"]) == fingerprint:
            self.stats["skipped"] += 1
            return
        self.fingerprints[row["id"]] = fingerprint
        self.emitted += 1
        yield row

    def _forget(self, kind: str, listed: set[str]) -> Iterator[dict]:
        """Tombstone the stored ids of a kind that its complete listing no longer holds."""
        prefix = self._prefix(kind)
        for row_id in sorted(self.fingerprints):
            if row_id.startswith(prefix) and row_id not in listed:
                del self.fingerprints[row_id]
                self.stats["deleted"] += 1
                yield {"id": row_id, "_deleted": True}

    def rows(self) -> Iterator[dict]:
        for kind in KINDS:
            if kind not in self.include:
                # deselected kinds leave memory
                yield from self._forget(kind, set())
                continue
            rows = [_RENDERERS[kind](record) for record in self._listing(kind)]
            for row in sorted(rows, key=lambda row: row["id"]):
                yield from self._changed(row)
            yield from self._forget(kind, {row["id"] for row in rows})


def _iter_rows(
    client: Any,
    property_id: str,
    state: dict,
    stats: dict[str, int],
    *,
    include: tuple[str, ...] = KINDS,
) -> Iterator[dict]:
    """Yield the changed rows of a property. Pure of dlt, so tests drive it with a dict."""
    walker = _Walker(client, property_id, state, stats, include=include)
    try:
        yield from walker.rows()
    except _CutShortError as cut:
        stats["failed"] = max(1, stats.get("failed", 0))
        stats[f"failed_{cut.reason}"] = 1
        logger.warning("GA4 sync stopped early (%s); it resumes on the next run.", cut.reason)


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def ga4_source(
    property_id: str | int,
    *,
    credentials_path: str | None = None,
    service: Any = None,
    resource_name: str | None = None,
    include: tuple[str, ...] = KINDS,
):
    """Return a ``dlt`` resource that yields a GA4 property's documents for ``remember``.

    Args:
        property_id: The numeric property id (Admin → Property details), not the
            ``G-`` measurement id. ``"properties/123"`` is accepted too.
        credentials_path: Service-account key file. Without it, Application Default
            Credentials are used (``GOOGLE_APPLICATION_CREDENTIALS``).
        service: Pre-built client, mainly an injection point for tests.
        resource_name: Stable name of this connection, ``ga4_<property id>`` by default.
            It scopes the sync state and the cleanup of deleted records, so keep it
            fixed across runs.
        include: Which of ``"custom_dimensions"``, ``"custom_metrics"``,
            ``"key_events"``, ``"audiences"`` and ``"annotations"`` to sync.

    Returns:
        A ``dlt`` resource configured with ``primary_key="id"``,
        ``write_disposition="merge"`` and a ``_deleted`` hard-delete column.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError("GA4 sync requires cognee>=1.6.1 (table-scoped document cleanup).")
    property_id = _text(property_id).removeprefix("properties/")
    # the id goes into resource names and request paths
    if not (property_id.isascii() and property_id.isdigit()):
        raise ValueError("property_id must be the numeric GA4 property id, not a G- measurement id")
    include = tuple(include)
    if not include or set(include) - set(KINDS):
        raise ValueError(f"include must be a non-empty subset of {KINDS}")

    # built here so a missing or malformed key fails at construction
    client = (
        service
        if service is not None
        else GA4Client(build_admin_services(load_credentials(credentials_path)))
    )
    resource_name = resource_name or f"ga4_{property_id}"
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def ga4_property():
        stats.clear()
        stats.update(scanned=0, skipped=0, deleted=0, failed=0)
        yield from _iter_rows(
            client, property_id, dlt.current.resource_state(), stats, include=include
        )

    resource = ga4_property()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "ga4")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    resource.cognee_sync_stats = stats
    return resource
