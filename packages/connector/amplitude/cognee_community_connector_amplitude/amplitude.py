"""Amplitude connector for cognee: a dlt resource that syncs your analytics metadata into memory.

It ingests the event taxonomy, cohort definitions, chart annotations and the saved
charts you name, never raw events, and is meant to be handed to ``cognee.remember``::

    await cognee.remember(
        amplitude_source(api_key="<api key>", secret_key="<secret key>"),
        dataset_name="amplitude",
        primary_key="id",
        write_disposition="merge",  # required, the add pipeline defaults to "replace"
    )
"""

import csv
import hashlib
import json
import logging
import time
from collections.abc import Callable, Iterator
from typing import Any

import requests
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = logging.getLogger(__name__)

HOSTS = {"us": "https://amplitude.com", "eu": "https://analytics.eu.amplitude.com"}
KINDS = ("events", "user_properties", "cohorts", "annotations")
DEFAULT_MAX_REQUESTS = 1000
_PREFIXES = {
    "events": "event",
    "user_properties": "user_property",
    "cohorts": "cohort",
    "annotations": "annotation",
    "charts": "chart",
}
# custom user properties carry this prefix, amplitude's built-in ones do not
_CUSTOM_PROPERTY = "gp:"
# amplitude's internal names for the cohort types
_COHORT_TYPES = {"redshift": "behavioral", "manual_upload": "uploaded list"}
# amplitude answers 403, not 401, to a wrong key, secret or region
_BAD_CREDENTIALS = "invalid api"
_TIMEOUT = (10, 30)
_RETRIES = 3


class AmplitudeSourceError(RuntimeError):
    """Base class of the errors this source raises."""


class AmplitudeAPIError(AmplitudeSourceError):
    """Amplitude answered with an error or an unexpected shape, or could not be reached."""


class AmplitudeAuthError(AmplitudeSourceError):
    """Amplitude rejected the API key, the secret key or the region."""


class AmplitudeAccessError(AmplitudeSourceError):
    """The plan or key cannot call an endpoint (HTTP 403)."""


class AmplitudeRateLimitedError(AmplitudeSourceError):
    """Amplitude answered HTTP 429."""


class AmplitudeNotFoundError(AmplitudeAPIError):
    """The record was deleted or never existed (HTTP 404)."""


def _credential(value: str | None, label: str) -> str:
    text = (value or "").strip()
    # reject early and without echoing the value: requests would put it in its exception
    if not text or not text.isascii() or not text.isprintable() or " " in text:
        raise ValueError(f"The Amplitude {label} is empty or has an invalid format")
    return text


def _bad_credentials(payload: Any) -> bool:
    """Whether a 403 is about the credentials, not the plan. Only matched, never echoed."""
    error = payload.get("error") if isinstance(payload, dict) else None
    metadata = error.get("metadata") if isinstance(error, dict) else None
    details = metadata.get("details") if isinstance(metadata, dict) else None
    return isinstance(details, str) and _BAD_CREDENTIALS in details.lower()


class AmplitudeClient:
    """Minimal synchronous, read-only REST client for Amplitude.

    Tests can pass any object with the same ``get`` method instead.
    """

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        *,
        region: str = "us",
        sleep: Callable[[float], None] = time.sleep,
    ):
        if region not in HOSTS:
            raise ValueError(f"region must be one of {tuple(HOSTS)}")
        self._auth = (_credential(api_key, "API key"), _credential(secret_key, "secret key"))
        self._base_url = HOSTS[region]
        self._sleep = sleep

    def __repr__(self) -> str:
        return "AmplitudeClient(api_key=<redacted>, secret_key=<redacted>)"

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        for attempt in range(_RETRIES):
            last = attempt == _RETRIES - 1
            try:
                response = requests.get(
                    self._base_url + path, params=params, auth=self._auth, timeout=_TIMEOUT
                )
            except requests.RequestException as exc:
                if last:
                    raise AmplitudeAPIError(
                        f"Amplitude request failed: {type(exc).__name__}"
                    ) from None
                self._sleep(2**attempt)
                continue
            if response.status_code >= 500 and not last:
                self._sleep(2**attempt)
                continue
            return self._parse(response, path)
        raise AmplitudeAPIError("Amplitude request failed")  # pragma: no cover

    def _parse(self, response: Any, path: str) -> dict[str, Any]:
        status = response.status_code
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if status == 401 or (status == 403 and _bad_credentials(payload)):
            raise AmplitudeAuthError(
                f"Amplitude rejected the API key or secret key (HTTP {status}); "
                "check both and the region"
            )
        if status == 403:
            raise AmplitudeAccessError(f"The Amplitude plan or key cannot call {path} (HTTP 403)")
        if status == 429:
            raise AmplitudeRateLimitedError("Amplitude rate limit exceeded (HTTP 429)")
        if status == 404:
            raise AmplitudeNotFoundError("Amplitude record not found (HTTP 404)")
        if status != 200:
            raise AmplitudeAPIError(f"Amplitude request failed: HTTP {status}")
        if not isinstance(payload, dict):
            raise AmplitudeAPIError(f"Amplitude answered {path} without a JSON object")
        return payload


# ---------------------------------------------------------------------------
# Rendering: deterministic, definitions only, nothing volatile.
# ---------------------------------------------------------------------------
def _text(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(sorted(_text(item) for item in value if _text(item)))
    return "" if value is None else str(value).strip()


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


def _traits(prop: dict[str, Any]) -> str:
    """A property's type and constraints on one line, e.g. ``enum (EUR, USD); required``."""
    kind = _text(prop.get("type"))
    kind = "" if kind == "any" else kind
    values = _text(prop.get("enum_values"))
    pattern = _text(prop.get("regex"))
    traits = [
        f"{kind} ({values})".strip() if values else kind,
        "array" if prop.get("is_array_type") else "",
        "required" if prop.get("is_required") else "",
        f"pattern {pattern}" if pattern else "",
        _text(prop.get("classifications")),
    ]
    return "; ".join(trait for trait in traits if trait)


def _property_line(prop: dict[str, Any]) -> str:
    details = [_traits(prop), _text(prop.get("description"))]
    known = ". ".join(detail for detail in details if detail)
    name = _text(prop.get("event_property"))
    return f"- {name}: {known}" if known else f"- {name}"


def render_event(event: dict[str, Any], properties: list[dict[str, Any]]) -> dict[str, Any]:
    event_type = _text(event.get("event_type"))
    fields = [
        ("Event type", event_type),
        ("Category", _text((event.get("category") or {}).get("name"))),
        ("Owner", _text(event.get("owner"))),
        ("Tags", _text(event.get("tags"))),
        ("Status", "" if event.get("is_active", True) else "inactive"),
    ]
    lines = sorted(_property_line(prop) for prop in properties)
    return _document(
        f"event:{event_type}",
        _text(event.get("display_name")) or event_type,
        fields,
        [_text(event.get("description"))],
        ["Properties:", *lines] if lines else [],
    )


def render_user_property(prop: dict[str, Any]) -> dict[str, Any]:
    raw_name = _text(prop.get("user_property"))
    name = raw_name.removeprefix(_CUSTOM_PROPERTY)
    fields = [("User property", name), ("Type", _traits(prop))]
    return _document(f"user_property:{raw_name}", name, fields, [_text(prop.get("description"))])


def _prune(value: Any) -> Any:
    """Drop the empty parts of a cohort definition so only its rules are left."""
    if isinstance(value, dict):
        pruned = {key: _prune(item) for key, item in value.items()}
        return {key: item for key, item in pruned.items() if item not in (None, "", [], {})}
    if isinstance(value, list):
        return [item for item in (_prune(item) for item in value) if item not in (None, "", [], {})]
    return value


def render_cohort(cohort: dict[str, Any]) -> dict[str, Any]:
    fields = [
        ("Cohort type", _COHORT_TYPES.get(cohort.get("type"), _text(cohort.get("type")))),
        ("Owners", _text(cohort.get("owners"))),
        ("Status", "archived" if cohort.get("archived") else ""),
    ]
    definition = _prune(cohort.get("definition") or {})
    # an uploaded id list has no clauses, so its definition says nothing
    has_rules = definition.get("andClauses") or definition.get("orClauses")
    rules = json.dumps(definition, sort_keys=True) if has_rules else ""
    return _document(
        f"cohort:{cohort['id']}",
        _text(cohort.get("name")) or "Untitled cohort",
        fields,
        [_text(cohort.get("description"))],
        ["Definition:", rules] if rules else [],
    )


def render_annotation(annotation: dict[str, Any]) -> dict[str, Any]:
    category = annotation.get("category") or {}
    start, end = _text(annotation.get("start")), _text(annotation.get("end"))
    fields = [
        ("Annotation date", f"{start} to {end}" if end else start),
        # the listing names the category "category", the docs "name"
        ("Category", _text(category.get("name") or category.get("category"))),
        ("Chart", _text(annotation.get("chart_id"))),
    ]
    return _document(
        f"annotation:{annotation['id']}",
        _text(annotation.get("label")) or "Untitled annotation",
        fields,
        [_text(annotation.get("details"))],
    )


def _chart_header(export: str) -> list[str]:
    """The lines above the results in a chart's CSV export: its title, measure and events."""
    header = []
    for cells in csv.reader(export.splitlines()):
        if len(cells) > 1:
            break
        header.extend(cell.strip() for cell in cells if cell.strip())
    return header


def render_chart(chart_id: str, export: str) -> dict[str, Any]:
    title, *definition = _chart_header(export) or ["Untitled chart"]
    return _document(
        f"chart:{chart_id}",
        title,
        [("Chart", chart_id)],
        ["Definition:", *definition] if definition else [],
    )


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
    """One run over the selected kinds, writing progress into ``state``.

    Amplitude has no change feed for this metadata, so every run lists each kind in
    full and emits a row only when its fingerprint changed. An id that a complete
    listing no longer holds is forgotten.
    """

    def __init__(
        self,
        client: Any,
        state: dict,
        stats: dict[str, int],
        *,
        include: tuple[str, ...],
        chart_ids: tuple[str, ...],
        include_archived: bool,
        max_requests: int,
    ):
        self.client = client
        self.state = state
        self.stats = stats
        for key in ("scanned", "skipped", "deleted", "failed", "no_access"):
            stats.setdefault(key, 0)
        # charts cannot be listed, so naming chart ids is what selects them
        self.include = (*include, "charts") if chart_ids else include
        self.chart_ids = chart_ids
        self.include_archived = include_archived
        self.max_requests = max_requests
        self.requests = 0
        self.emitted = 0
        self.fingerprints: dict[str, str] = state.setdefault("fingerprints", {})

    # -- requests ----------------------------------------------------------
    def _call(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.requests >= self.max_requests:
            raise _CutShortError("budget")
        self.requests += 1
        try:
            return self.client.get(path, params)
        except AmplitudeRateLimitedError:
            raise _CutShortError("rate_limit") from None
        except AmplitudeAuthError:
            if self.emitted:
                raise _CutShortError("auth") from None
            raise

    def _listing(
        self, path: str, key: str, id_field: str, params: dict[str, Any] | None = None
    ) -> list[dict]:
        """Return a listing's records, refusing any shape a delete could not be trusted on."""
        payload = self._call(path, params)
        records = payload.get(key)
        complete = (
            payload.get("success", True) is True
            and isinstance(records, list)
            and all(isinstance(record, dict) and _text(record.get(id_field)) for record in records)
        )
        if not complete:
            raise AmplitudeAPIError(f"Amplitude answered {path} with an unexpected shape")
        return records

    # -- rows --------------------------------------------------------------
    def _changed(self, row: dict) -> Iterator[dict]:
        self.stats["scanned"] += 1
        fingerprint = _hash(row)
        if self.fingerprints.get(row["id"]) == fingerprint:
            self.stats["skipped"] += 1
            return
        self.fingerprints[row["id"]] = fingerprint
        self.emitted += 1
        yield row

    def _tombstone(self, row_id: str) -> Iterator[dict]:
        if self.fingerprints.pop(row_id, None) is not None:
            self.stats["deleted"] += 1
            yield {"id": row_id, "_deleted": True}

    def _forget(self, kind: str, listed: set[str]) -> Iterator[dict]:
        """Tombstone the stored ids of a kind that its complete listing no longer holds."""
        prefix = f"{_PREFIXES[kind]}:"
        for row_id in sorted(self.fingerprints):
            if row_id.startswith(prefix) and row_id not in listed:
                yield from self._tombstone(row_id)

    def _sync(self, kind: str, rows: list[dict]) -> Iterator[dict]:
        for row in sorted(rows, key=lambda row: row["id"]):
            yield from self._changed(row)
        yield from self._forget(kind, {row["id"] for row in rows})

    # -- kinds -------------------------------------------------------------
    def _events(self) -> Iterator[dict]:
        listed = self._listing("/api/2/taxonomy/event", "data", "event_type")
        events = {f"event:{_text(event['event_type'])}": event for event in listed}
        # properties cost one request per event, so a cut run resumes after the events it rendered
        rendered: list[str] = self.state.setdefault("events_rendered", [])
        for row_id in sorted(set(events) - set(rendered)):
            properties = self._listing(
                "/api/2/taxonomy/event-property",
                "data",
                "event_property",
                {"event_type": events[row_id]["event_type"]},
            )
            yield from self._changed(render_event(events[row_id], properties))
            rendered.append(row_id)
        yield from self._forget("events", set(events))
        rendered.clear()

    def _user_properties(self) -> Iterator[dict]:
        properties = self._listing("/api/2/taxonomy/user-property", "data", "user_property")
        yield from self._sync(
            "user_properties",
            [
                render_user_property(prop)
                for prop in properties
                # built-in properties (user_id, device_id, ...) say nothing unless documented
                if _text(prop["user_property"]).startswith(_CUSTOM_PROPERTY)
                or _text(prop.get("description"))
            ],
        )

    def _cohorts(self) -> Iterator[dict]:
        cohorts = self._listing("/api/3/cohorts", "cohorts", "id")
        yield from self._sync(
            "cohorts",
            [render_cohort(c) for c in cohorts if self.include_archived or not c.get("archived")],
        )

    def _annotations(self) -> Iterator[dict]:
        annotations = self._listing("/api/3/annotations", "data", "id")
        yield from self._sync("annotations", [render_annotation(a) for a in annotations])

    def _charts(self) -> Iterator[dict]:
        charts = {f"chart:{chart_id}": chart_id for chart_id in self.chart_ids}
        # every chart costs a query, so a cut run resumes after the charts it rendered
        rendered: list[str] = self.state.setdefault("charts_rendered", [])
        for row_id in sorted(set(charts) - set(rendered)):
            try:
                export = self._call(f"/api/3/chart/{charts[row_id]}/csv").get("data")
            except AmplitudeNotFoundError:
                logger.warning("Amplitude chart %s was not found; it is left out.", charts[row_id])
                yield from self._tombstone(row_id)
            else:
                if not isinstance(export, str):
                    raise AmplitudeAPIError("Amplitude answered a chart with an unexpected shape")
                yield from self._changed(render_chart(charts[row_id], export))
            rendered.append(row_id)
        yield from self._forget("charts", set(charts))
        rendered.clear()

    # -- the run -----------------------------------------------------------
    def rows(self) -> Iterator[dict]:
        walks = {
            "events": self._events,
            "user_properties": self._user_properties,
            "cohorts": self._cohorts,
            "annotations": self._annotations,
            "charts": self._charts,
        }
        for kind in walks:
            if kind not in self.include:
                # deselected kinds leave memory
                self.state.pop(f"{kind}_rendered", None)
                yield from self._forget(kind, set())
                continue
            try:
                yield from walks[kind]()
            except AmplitudeAccessError:
                # no access is not a deletion: the kind's documents stay
                self.stats["no_access"] += 1
                logger.warning(
                    "Amplitude sync skipped %s: the plan or key cannot read it (HTTP 403).", kind
                )


def _iter_rows(
    client: Any,
    state: dict,
    stats: dict[str, int],
    *,
    include: tuple[str, ...] = KINDS,
    chart_ids: tuple[str, ...] = (),
    include_archived: bool = False,
    max_requests: int = DEFAULT_MAX_REQUESTS,
) -> Iterator[dict]:
    """Yield the changed rows of a project. Pure of dlt, so tests drive it with a dict."""
    walker = _Walker(
        client,
        state,
        stats,
        include=include,
        chart_ids=chart_ids,
        include_archived=include_archived,
        max_requests=max_requests,
    )
    try:
        yield from walker.rows()
    except _CutShortError as cut:
        stats["failed"] = max(1, stats.get("failed", 0))
        stats[f"failed_{cut.reason}"] = 1
        logger.warning("Amplitude sync stopped early (%s); it resumes on the next run.", cut.reason)


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def amplitude_source(
    *,
    api_key: str | None = None,
    secret_key: str | None = None,
    region: str = "us",
    service: Any = None,
    resource_name: str = "amplitude",
    include: tuple[str, ...] = KINDS,
    chart_ids: list[str] | None = None,
    include_archived: bool = False,
    max_requests: int = DEFAULT_MAX_REQUESTS,
):
    """Return a ``dlt`` resource that yields an Amplitude project's documents for ``remember``.

    Args:
        api_key, secret_key: The project's API key and secret key. Used to build the
            client when ``service`` is omitted.
        region: ``"us"`` or ``"eu"``, the project's data region.
        service: Pre-built client, mainly an injection point for tests.
        resource_name: Stable name of this connection. It scopes the sync state and
            the cleanup of deleted records, so keep it fixed across runs.
        include: Which of ``"events"``, ``"user_properties"``, ``"cohorts"`` and
            ``"annotations"`` to sync.
        chart_ids: Ids of saved charts to sync as well, taken from their URLs. Amplitude
            cannot list charts with these keys, and reading one runs its query.
        include_archived: Keep archived cohorts. By default archiving one forgets it.
        max_requests: Request budget of one run. A bigger taxonomy finishes over
            several runs.

    Returns:
        A ``dlt`` resource configured with ``primary_key="id"``,
        ``write_disposition="merge"`` and a ``_deleted`` hard-delete column.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError("Amplitude sync requires cognee>=1.6.1 (table-scoped document cleanup).")
    if service is None and not (api_key and secret_key):
        raise ValueError("amplitude_source needs a service, or an api_key and a secret_key")
    include = tuple(include)
    if not include or set(include) - set(KINDS):
        raise ValueError(f"include must be a non-empty subset of {KINDS}")
    chart_ids = tuple(chart_ids or ())
    # the ids go into request paths
    if not all(isinstance(i, str) and i.isascii() and i.isalnum() for i in chart_ids):
        raise ValueError("chart_ids must be the alphanumeric ids from chart URLs")

    # built here so malformed credentials fail at construction and the closure holds no raw key
    client = (
        service
        if service is not None
        else AmplitudeClient(api_key or "", secret_key or "", region=region)
    )
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def amplitude_project():
        stats.clear()
        stats.update(scanned=0, skipped=0, deleted=0, failed=0, no_access=0)
        yield from _iter_rows(
            client,
            dlt.current.resource_state(),
            stats,
            include=include,
            chart_ids=chart_ids,
            include_archived=include_archived,
            max_requests=max_requests,
        )

    resource = amplitude_project()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "amplitude")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    resource.cognee_sync_stats = stats
    return resource
