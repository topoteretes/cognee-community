"""Wikidata connector for cognee, a ``dlt`` source that turns Wikidata items into memory.

Sync a selection of Wikidata (or any Wikibase) items into cognee, incrementally
and with forget-on-deletion::

    import cognee
    from cognee_community_connector_wikidata import wikidata_source

    await cognee.remember(
        wikidata_source(items=["Q42"], expand_properties=["P800"]),
        dataset_name="wikidata",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

Design
------
* **Transport**: the Wikibase Action API (``wbgetentities``) and, optionally,
  the SPARQL query service, both over plain ``httpx`` (a core cognee
  dependency), so the connector adds no new dependency. No credentials are
  needed. Requests are sequential, carry a descriptive User-Agent (Wikimedia
  policy) and ``maxlag``, and are retried on maxlag, HTTP 429 and 5xx,
  honouring ``Retry-After``.
* **Selection**: seed ids (``items``), optionally expanded along an allowlist
  of properties (``expand_properties``) to a bounded hop depth, and/or the
  items a SPARQL query returns. ``max_items`` caps the whole selection, so a
  seed such as ``Q5`` (human) cannot pull in millions of items.
* **One document per item**, keyed by its id: the label, description and
  aliases, then one readable line per statement (``occupation: playwright
  (start time: 1979)``), with property names, item values and units resolved
  to labels in the configured language. Labels are always requested with
  language fallback: Wikidata stores many labels only under the ``mul``
  (all languages) code, so a plain ``languages=en`` request returns no label
  at all for items such as Douglas Adams. Dates are rendered at their stated
  precision (``1979``, not ``1979-00-00``), so TEMPORAL search can use them.
  External identifiers (about three quarters of the statements on a typical
  well-known item) and deprecated statements are left out by default.
  Sitelinks are kept, so items line up with pages ingested by the MediaWiki
  connector. Each item carries the ``wikidata:<server>`` node set.
* **Incremental**: every run asks ``wbgetentities`` for the revision of each
  selected item, 50 ids per request, and fetches and re-renders only items
  whose ``lastrevid`` moved. An unchanged selection of 500 items costs about
  ten requests and no rendering.
* **Forget-on-delete**: an item that left the selection, was deleted upstream
  (``missing``), or was merged into another item (a redirect) is emitted as a
  ``_deleted`` tombstone; a merged item's target is synced in its place. dlt
  drops the tombstone on ``merge`` and cognee's ``orphan_cleanup`` removes it
  from the graph, vector and relational stores. A selection that comes back
  empty while items were synced before deletes nothing, so a broken SPARQL
  query or an outage cannot wipe the dataset.

Every API failure raises before the run's state is saved, so dlt keeps the
previous state and the next run retries.
"""

from __future__ import annotations

import contextlib
import re
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("wikidata_connector")

SOURCE_TAG = "wikidata"
DEFAULT_API_URL = "https://www.wikidata.org/w/api.php"
DEFAULT_SPARQL_URL = "https://query.wikidata.org/sparql"
DEFAULT_USER_AGENT = (
    "cognee-community-connector-wikidata/0.1.0 (+https://github.com/topoteretes/cognee-community)"
)

# wbgetentities accepts at most 50 ids per request for normal users
# (ApiBase::LIMIT_SML1; bots get 500, which this connector does not rely on).
_BATCH_SIZE = 50
# Attempts per request on 429 / 5xx / maxlag / network errors: our choice, so a
# lagging service costs at most a few minutes before the run stops cleanly.
_MAX_RETRIES = 5
# Longest wait honoured for Retry-After / maxlag. The sync runs while cognee holds
# its process-wide dlt staging lock, so a service asking for more stops the run
# cleanly (state untouched, retried next run) instead of sleeping inside the lock.
_MAX_RETRY_AFTER_SECONDS = 60.0
_RATE_LIMIT_API_ERRORS = {"maxlag", "ratelimited"}
_ENTITY_ID_RE = re.compile(r"^[QP][1-9]\d*$")
# Captures an id at the end of an entity URI or page URL, e.g.
# http://www.wikidata.org/entity/Q42 or https://www.wikidata.org/wiki/Property:P31.
_TRAILING_ID_RE = re.compile(r"(?:^|[/:])([QqPp][1-9]\d*)$")
# Wikibase time precisions below "year" (decade, century, millennium, ...).
_PRECISION_DAY, _PRECISION_MONTH, _PRECISION_YEAR = 11, 10, 9
_PRECISION_DECADE, _PRECISION_CENTURY, _PRECISION_MILLENNIUM = 8, 7, 6
_JULIAN_CALENDAR = "Q1985786"


class WikidataAPIError(RuntimeError):
    """The API answered with an error (``{"error": {...}}``) or bad HTTP."""

    def __init__(self, code: str, info: str = "", entity_id: str | None = None):
        super().__init__(f"Wikidata API error {code}: {info}" if info else code)
        self.code = code
        self.info = info
        self.entity_id = entity_id


class WikidataRateLimitedError(WikidataAPIError):
    """The service kept asking to slow down (429, maxlag) beyond what a run waits."""


class WikidataAuthError(WikidataAPIError):
    """The service refused access (HTTP 401/403, e.g. a blocked User-Agent)."""


class WikidataScopeError(ValueError):
    """A configured seed id does not exist (likely a typo)."""


def normalize_entity_id(value: str) -> str:
    """Return the bare id (``Q42`` / ``P31``) for an id, entity URI or page URL."""
    match = _TRAILING_ID_RE.search(str(value).strip().rstrip("/"))
    if not match:
        raise ValueError(f"Not a Wikidata item or property id: {value!r}")
    return match.group(1).upper()


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------
class _WikibaseClient:
    """Thin, sequential wrapper over ``api.php`` and the SPARQL endpoint."""

    def __init__(
        self,
        http_client: Any,
        api_url: str,
        user_agent: str,
        maxlag: int | None,
        sparql_url: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._http = http_client
        self.api_url = api_url
        self._sparql_url = sparql_url
        self._user_agent = user_agent
        self._maxlag = maxlag
        self._sleep = sleep
        self.requests = 0

    # -- transport ------------------------------------------------------------
    def _send(self, url: str, params: dict[str, Any], accept: str) -> Any:
        """GET with retries on network errors, 429 and 5xx; return the response."""
        import httpx

        headers = {"User-Agent": self._user_agent, "Accept": accept}
        attempt = 0
        while True:
            last_attempt = attempt >= _MAX_RETRIES - 1
            self.requests += 1
            try:
                response = self._http.get(url, params=params, headers=headers)
            except httpx.TransportError as exc:
                if last_attempt:
                    raise
                self._backoff(f"network error ({type(exc).__name__})", None, attempt)
                attempt += 1
                continue
            status = response.status_code
            if status == 429 or status >= 500:
                if last_attempt:
                    error_class = WikidataRateLimitedError if status == 429 else WikidataAPIError
                    raise error_class(f"http_{status}")
                self._backoff(f"HTTP {status}", response.headers, attempt, rate_limit=status == 429)
                attempt += 1
                continue
            if status in (401, 403):
                raise WikidataAuthError(f"http_{status}")
            if status >= 400:
                raise WikidataAPIError(f"http_{status}", response.text[:200])
            return response

    def _backoff(self, reason: str, headers: Any, attempt: int, rate_limit: bool = False) -> None:
        delay = _retry_after(headers, attempt)
        if delay > _MAX_RETRY_AFTER_SECONDS:
            error_class = WikidataRateLimitedError if rate_limit else WikidataAPIError
            raise error_class(
                "retry_after_too_long",
                f"asked to wait {delay:.0f}s (more than {_MAX_RETRY_AFTER_SECONDS:.0f}s)",
            )
        logger.warning(
            "Wikidata: %s, retrying in %.1fs (%d/%d).", reason, delay, attempt + 1, _MAX_RETRIES
        )
        self._sleep(delay)

    def api(self, params: dict[str, Any]) -> dict:
        full = {"format": "json", "formatversion": "2", **params}
        if self._maxlag is not None:
            full["maxlag"] = str(self._maxlag)
        attempt = 0
        while True:
            response = self._send(self.api_url, full, "application/json")
            try:
                payload = response.json()
            except ValueError:
                # A proxy or maintenance page answering 200 with HTML: treat it as
                # a failure, never as an empty result.
                raise WikidataAPIError("invalid_response", "the API did not answer JSON") from None
            if not isinstance(payload, dict):
                raise WikidataAPIError("invalid_response", "the API did not answer an object")
            error = payload.get("error")
            if not error:
                return payload
            code = str(error.get("code") or "unknown")
            info = str(error.get("info") or "")
            if code in _RATE_LIMIT_API_ERRORS:
                if attempt >= _MAX_RETRIES - 1:
                    raise WikidataRateLimitedError(code, info)
                self._backoff(code, response.headers, attempt, rate_limit=True)
                attempt += 1
                continue
            raise WikidataAPIError(code, info, error.get("id"))

    def sparql(self, query: str) -> list[dict]:
        if not self._sparql_url:
            raise ValueError("No SPARQL endpoint configured (pass sparql_url=).")
        response = self._send(
            self._sparql_url,
            {"query": query, "format": "json"},
            "application/sparql-results+json",
        )
        try:
            return response.json()["results"]["bindings"]
        except (ValueError, KeyError, TypeError) as exc:
            # The query service reports timeouts and syntax errors as a text
            # body (sometimes with a 200), not as JSON results.
            raise WikidataAPIError("sparql_bad_response", response.text[:200]) from exc

    # -- wbgetentities --------------------------------------------------------
    def get_entities(self, ids: Iterable[str], params: dict[str, Any]) -> dict[str, dict]:
        """``wbgetentities`` for any number of ids, keyed by the requested id.

        A deleted entity comes back as ``{"missing": ""}``; a merged one under
        the id that was asked for, carrying ``redirects`` and the target's id.
        An id that was never assigned fails the whole request with
        ``no-such-entity``; it is reported as missing and the request retried
        without it, so one bad seed cannot block the rest.
        """
        entities: dict[str, dict] = {}
        for batch in _batched(sorted(set(ids))):
            pending = list(batch)
            while pending:
                try:
                    response = self.api(
                        {"action": "wbgetentities", "ids": "|".join(pending), **params}
                    )
                except WikidataAPIError as exc:
                    if exc.code == "no-such-entity" and exc.entity_id in pending:
                        entities[exc.entity_id] = {"id": exc.entity_id, "missing": ""}
                        pending.remove(exc.entity_id)
                        continue
                    raise
                for requested, entity in (response.get("entities") or {}).items():
                    entities[requested] = entity
                pending = []
        return entities


def _retry_after(headers: Any, attempt: int) -> float:
    """Seconds to wait before retrying: the Retry-After header, else backoff."""
    header = (headers or {}).get("retry-after") if headers is not None else None
    try:
        return max(float(header), 0.0)
    except (TypeError, ValueError):
        return min(float(2**attempt), _MAX_RETRY_AFTER_SECONDS)


def _batched(values: Iterable[Any], size: int = _BATCH_SIZE) -> Iterator[list]:
    batch: list = []
    for value in values:
        batch.append(value)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def _is_missing(entity: dict | None) -> bool:
    # wbgetentities always answers "missing": "" (formatversion is ignored).
    return entity is None or "missing" in entity


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Settings:
    items: tuple[str, ...]
    expand_properties: tuple[str, ...]
    expand_depth: int
    sparql: str | None
    sparql_variable: str
    max_items: int
    language: str
    include_external_ids: bool
    sitelinks: tuple[str, ...]
    full_sync: bool

    def selection_fingerprint(self) -> list:
        return [
            sorted(self.items),
            sorted(self.expand_properties),
            self.expand_depth,
            self.sparql,
            self.sparql_variable,
            self.max_items,
        ]

    def render_fingerprint(self) -> list:
        return [self.language, self.include_external_ids, sorted(self.sitelinks)]


def _entity_params(settings: _Settings) -> dict[str, str]:
    params = {
        "props": "info|labels|descriptions|aliases|claims|sitelinks/urls",
        "languages": settings.language,
        "languagefallback": "1",
    }
    if settings.sitelinks:
        params["sitefilter"] = "|".join(settings.sitelinks)
    return params


_INFO_PARAMS = {"props": "info"}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _ordinal(number: int) -> str:
    if 10 <= number % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def format_time(value: dict) -> str:
    """Render a Wikibase time value at its precision (``1979``, ``1952-03-11``)."""
    raw = str(value.get("time") or "")
    match = re.match(r"^([+-])(\d+)-(\d{2})-(\d{2})", raw)
    if not match:
        return raw
    sign, year_text, month, day = match.groups()
    year = int(year_text)
    bce = sign == "-"
    precision = int(value.get("precision", _PRECISION_DAY))
    era = " BCE" if bce else ""

    if precision >= _PRECISION_DAY and month != "00" and day != "00":
        text = f"{year:04d}-{month}-{day}{era}"
    elif precision >= _PRECISION_MONTH and month != "00":
        text = f"{year:04d}-{month}{era}"
    elif precision >= _PRECISION_YEAR:
        text = f"{year}{era}"
    elif precision == _PRECISION_DECADE:
        text = f"{year // 10 * 10}s{era}"
    elif precision == _PRECISION_CENTURY:
        text = f"{_ordinal((year - 1) // 100 + 1)} century{era}"
    elif precision == _PRECISION_MILLENNIUM:
        text = f"{_ordinal((year - 1) // 1000 + 1)} millennium{era}"
    else:
        text = f"about {year} years{' ago' if bce else ''}" if year >= 10_000 else f"{year}{era}"
    if str(value.get("calendarmodel") or "").endswith(_JULIAN_CALENDAR):
        text += " (Julian calendar)"
    return text


def _amount(raw: Any) -> str:
    text = str(raw)
    return text[1:] if text.startswith("+") else text


@dataclass
class _Labels:
    """Batched, per-run label cache for properties, items and units."""

    client: _WikibaseClient
    language: str
    cache: dict[str, str] = field(default_factory=dict)

    def prefetch(self, ids: Iterable[str]) -> None:
        wanted = {entity_id for entity_id in ids if entity_id not in self.cache}
        if not wanted:
            return
        entities = self.client.get_entities(
            wanted,
            {"props": "labels", "languages": self.language, "languagefallback": "1"},
        )
        for entity_id in wanted:
            label = _label(entities.get(entity_id) or {}, self.language)
            self.cache[entity_id] = label or entity_id

    def get(self, entity_id: str) -> str:
        return self.cache.get(entity_id, entity_id)


def _label(entity: dict, language: str) -> str:
    return ((entity.get("labels") or {}).get(language) or {}).get("value") or ""


def _referenced_ids(entity: dict, settings: _Settings) -> set[str]:
    """Every property, item value and unit a document will name."""
    ids: set[str] = set()
    for property_id, statements in (entity.get("claims") or {}).items():
        kept = [s for s in statements if _keep_statement(s, settings)]
        if not kept:
            continue
        ids.add(property_id)
        for statement in kept:
            snaks = [statement.get("mainsnak") or {}]
            for qualifier_id, qualifier_snaks in (statement.get("qualifiers") or {}).items():
                ids.add(qualifier_id)
                snaks.extend(qualifier_snaks)
            for snak in snaks:
                ids.update(_snak_entity_ids(snak))
    return ids


def _snak_entity_ids(snak: dict) -> set[str]:
    value = (snak.get("datavalue") or {}).get("value")
    if not isinstance(value, dict):
        return set()
    found = set()
    if value.get("id"):
        found.add(str(value["id"]))
    unit = str(value.get("unit") or "")
    if unit and unit != "1":
        with contextlib.suppress(ValueError):
            found.add(normalize_entity_id(unit))
    return found


def _keep_statement(statement: dict, settings: _Settings) -> bool:
    if statement.get("rank") == "deprecated":
        return False
    datatype = (statement.get("mainsnak") or {}).get("datatype")
    return settings.include_external_ids or datatype != "external-id"


def format_snak(snak: dict, labels: _Labels) -> str:
    """Render one snak's value as text, resolving entity ids to labels."""
    snak_type = snak.get("snaktype")
    if snak_type == "somevalue":
        return "unknown value"
    if snak_type == "novalue":
        return "no value"
    datavalue = snak.get("datavalue") or {}
    value = datavalue.get("value")
    kind = datavalue.get("type")
    if kind == "wikibase-entityid":
        return labels.get(str(value.get("id")))
    if kind == "time":
        return format_time(value)
    if kind == "quantity":
        amount = _amount(value.get("amount"))
        unit = str(value.get("unit") or "1")
        if unit == "1":
            return amount
        try:
            return f"{amount} {labels.get(normalize_entity_id(unit))}"
        except ValueError:
            return f"{amount} {unit}"
    if kind == "monolingualtext":
        text = str(value.get("text") or "")
        language = value.get("language")
        return text if language == labels.language else f"{text} ({language})"
    if kind == "globecoordinate":
        return f"{value.get('latitude')}, {value.get('longitude')}"
    if isinstance(value, str):
        return value
    return str(value)


def _text_language(snak: dict) -> str | None:
    value = (snak.get("datavalue") or {}).get("value")
    return value.get("language") if isinstance(value, dict) and "text" in value else None


def _prefer_language(items: list, snak_of: Callable[[Any], dict], language: str) -> list:
    """Drop other-language texts when one in ``language`` exists.

    A caption or a name is often given in a dozen languages; repeating each
    translation adds noise, not facts. When none is in ``language``, all stay.
    """
    if not any(_text_language(snak_of(item)) == language for item in items):
        return items
    return [item for item in items if _text_language(snak_of(item)) in (None, language)]


def _render_statement(statement: dict, labels: _Labels) -> str:
    text = format_snak(statement.get("mainsnak") or {}, labels)
    qualifier_order = statement.get("qualifiers-order") or list(
        (statement.get("qualifiers") or {}).keys()
    )
    details = []
    for qualifier_id in qualifier_order:
        snaks = (statement.get("qualifiers") or {}).get(qualifier_id) or []
        values = [
            format_snak(snak, labels)
            for snak in _prefer_language(snaks, lambda snak: snak, labels.language)
        ]
        if values:
            details.append(f"{labels.get(qualifier_id)}: {', '.join(values)}")
    if details:
        text += f" ({'; '.join(details)})"
    return text


def render_document(entity: dict, labels: _Labels, settings: _Settings) -> tuple[str, str]:
    """Return ``(title, content)`` for one entity. Labels must be prefetched."""
    entity_id = entity["id"]
    language = settings.language
    label = _label(entity, language) or entity_id
    lines = [f"# {label} ({entity_id})"]
    description = ((entity.get("descriptions") or {}).get(language) or {}).get("value")
    if description:
        lines.append(description)
    aliases = [alias.get("value") for alias in (entity.get("aliases") or {}).get(language) or []]
    if aliases:
        lines.append("Also known as: " + ", ".join(a for a in aliases if a))

    statements: list[str] = []
    for property_id, property_statements in (entity.get("claims") or {}).items():
        kept = [s for s in property_statements if _keep_statement(s, settings)]
        kept = _prefer_language(kept, lambda s: s.get("mainsnak") or {}, language)
        # Preferred statements (the current value) first, then the rest.
        kept.sort(key=lambda s: s.get("rank") != "preferred")
        name = labels.get(property_id)
        statements.extend(f"- {name}: {_render_statement(s, labels)}" for s in kept)
    if statements:
        lines.append("\n## Statements\n" + "\n".join(statements))

    sitelinks = [
        f"- {site}: {link.get('title')}" + (f" ({link['url']})" if link.get("url") else "")
        for site, link in sorted((entity.get("sitelinks") or {}).items())
    ]
    if sitelinks:
        lines.append("\n## Sitelinks\n" + "\n".join(sitelinks))
    return label, "\n".join(lines)


def _expansion_links(entity: dict, settings: _Settings) -> list[str]:
    """Item ids this entity points at through the expansion properties."""
    links: list[str] = []
    for property_id in settings.expand_properties:
        for statement in (entity.get("claims") or {}).get(property_id) or []:
            if statement.get("rank") == "deprecated":
                continue
            value = ((statement.get("mainsnak") or {}).get("datavalue") or {}).get("value")
            entity_id = str(value.get("id") or "") if isinstance(value, dict) else ""
            if _ENTITY_ID_RE.match(entity_id) and entity_id not in links:
                links.append(entity_id)
    return links


def _tombstone(entity_id: str) -> dict:
    return {"id": entity_id, "_deleted": True}


# ---------------------------------------------------------------------------
# Sync (pure given a client + state dict, so unit-testable)
# ---------------------------------------------------------------------------
def _sparql_items(client: _WikibaseClient, settings: _Settings) -> list[str]:
    if not settings.sparql:
        return []
    found: list[str] = []
    for binding in client.sparql(settings.sparql):
        cell = binding.get(settings.sparql_variable) or {}
        if cell.get("type") != "uri":
            continue
        try:
            entity_id = normalize_entity_id(cell.get("value") or "")
        except ValueError:
            continue
        if entity_id not in found:
            found.append(entity_id)
    if not found:
        logger.warning(
            "Wikidata: the SPARQL query returned no ?%s entity URIs.", settings.sparql_variable
        )
    return found


@dataclass
class _Selection:
    """The items selected this run: canonical id -> (revision, entity or None)."""

    revisions: dict[str, int] = field(default_factory=dict)
    entities: dict[str, dict] = field(default_factory=dict)
    links: dict[str, list[str]] = field(default_factory=dict)
    redirected: dict[str, str] = field(default_factory=dict)
    missing: set[str] = field(default_factory=set)
    truncated: bool = False


def _select(
    client: _WikibaseClient, settings: _Settings, previous: dict[str, dict], refetch: bool
) -> _Selection:
    """Resolve roots and expansion hops; fetch full data only for changed items."""
    selection = _Selection()
    roots = list(dict.fromkeys([*settings.items, *_sparql_items(client, settings)]))
    entity_params = _entity_params(settings)
    frontier = roots
    depth = 0
    while frontier:
        room = settings.max_items - len(selection.revisions)
        if room <= 0:
            selection.truncated = True
            break
        info = client.get_entities(frontier, _INFO_PARAMS)
        changed: list[str] = []
        level: list[str] = []
        for requested in frontier:
            entity = info.get(requested)
            if _is_missing(entity):
                selection.missing.add(requested)
                continue
            canonical = str(entity["id"])
            if canonical != requested:
                selection.redirected[requested] = canonical
            if canonical in selection.revisions or canonical in level:
                continue
            if len(level) >= room:
                selection.truncated = True
                break
            level.append(canonical)
            revision = int(entity.get("lastrevid") or 0)
            selection.revisions[canonical] = revision
            seen = previous.get(canonical) or {}
            if refetch or seen.get("rev") != revision:
                changed.append(canonical)
            else:
                selection.links[canonical] = list(seen.get("links") or [])

        if changed:
            fetched = client.get_entities(changed, entity_params)
            for entity_id in changed:
                entity = fetched.get(entity_id)
                if _is_missing(entity):  # deleted between the two requests
                    selection.revisions.pop(entity_id, None)
                    selection.missing.add(entity_id)
                    continue
                selection.revisions[entity_id] = int(
                    entity.get("lastrevid") or selection.revisions[entity_id]
                )
                selection.entities[entity_id] = entity
                selection.links[entity_id] = _expansion_links(entity, settings)

        if depth >= settings.expand_depth or not settings.expand_properties:
            break
        depth += 1
        frontier = list(
            dict.fromkeys(
                link
                for entity_id in level
                if entity_id in selection.revisions
                for link in selection.links.get(entity_id) or []
                if link not in selection.revisions
            )
        )
    return selection


def _sync(client: _WikibaseClient, settings: _Settings, state: dict, stats: dict[str, int]):
    previous: dict[str, dict] = dict(state.get("items") or {})

    reason = None
    if not previous:
        reason = "initial"
    elif settings.full_sync:
        reason = "requested"
    elif state.get("render") != settings.render_fingerprint():
        reason = "render_changed"
    elif state.get("selection") != settings.selection_fingerprint():
        reason = "selection_changed"
    if reason:
        stats["full_sync"] = 1
        stats[f"full_sync_{reason}"] = 1

    selection = _select(client, settings, previous, refetch=reason is not None)

    # A seed that does not exist is a typo when it was just added to the
    # configuration; one synced before and deleted since is forgotten below.
    previous_selection = state.get("selection")
    known_seeds = (
        set(previous_selection[0] or [])
        if isinstance(previous_selection, list) and previous_selection
        else set()
    )
    unknown = sorted(selection.missing & (set(settings.items) - known_seeds))
    if unknown:
        raise WikidataScopeError(f"These ids do not exist (deleted or never assigned): {unknown}")

    stats["items_selected"] = len(selection.revisions)
    stats["redirected"] = len(selection.redirected)
    if selection.truncated:
        logger.warning(
            "Wikidata: the selection reached max_items=%d; further items were skipped.",
            settings.max_items,
        )
        stats["truncated"] = 1

    confirmed_gone = selection.missing | set(selection.redirected)
    if previous and not selection.revisions and not previous.keys() <= confirmed_gone:
        # An empty selection while items were synced before is far more likely
        # a broken SPARQL query or an outage than every item vanishing at once.
        # Forgetting the whole dataset would be permanent, so the run changes
        # nothing and the next one retries. When the API itself reported every
        # synced item missing (deleted upstream), the deletions are real and go
        # ahead.
        logger.warning(
            "Wikidata: the selection resolved to 0 items but %d were synced before; "
            "leaving the dataset and the sync state untouched.",
            len(previous),
        )
        stats["failed"] = 1
        stats["failed_empty_scope"] = 1
        return

    labels = _Labels(client, settings.language)
    labels.prefetch(
        entity_id
        for entity in selection.entities.values()
        for entity_id in _referenced_ids(entity, settings)
    )
    node_set = f"{SOURCE_TAG}:{urlparse(client.api_url).netloc}"
    synced: dict[str, dict] = {}
    for entity_id, revision in selection.revisions.items():
        record = {"rev": revision, "links": selection.links.get(entity_id) or []}
        entity = selection.entities.get(entity_id)
        if entity is None:
            synced[entity_id] = record
            stats["items_unchanged"] += 1
            continue
        title, content = render_document(entity, labels, settings)
        synced[entity_id] = record
        stats["items_changed"] += 1
        yield {
            "id": entity_id,
            "title": title,
            "content": content,
            "url": _entity_url(client.api_url, entity),
            "_deleted": False,
            dlt_utils.NODE_SET_COLUMN: [node_set],
        }

    for entity_id in sorted(previous.keys() - synced.keys()):
        stats["deleted"] += 1
        yield _tombstone(entity_id)

    # State is written only after the last row was yielded, so a run that raises
    # part-way keeps the previous state (dlt also discards the state of a failed
    # load).
    state["items"] = synced
    state["selection"] = settings.selection_fingerprint()
    state["render"] = settings.render_fingerprint()
    logger.info(
        "Wikidata: %s sync, %d changed, %d unchanged, %d deleted, %d request(s).",
        "full" if reason else "incremental",
        stats["items_changed"],
        stats["items_unchanged"],
        stats["deleted"],
        client.requests,
    )


def sync_items(client: _WikibaseClient, settings: _Settings, state: dict, stats: dict):
    """Yield changed item documents and deletion tombstones, then advance ``state``.

    ``stats`` receives plain int counters for the host: ``items_selected``,
    ``items_changed``, ``items_unchanged``, ``deleted``, ``redirected``,
    ``truncated``, ``requests``, ``full_sync`` (plus ``full_sync_<reason>``) and
    ``failed`` (plus ``failed_<reason>``: ``rate_limit``, ``auth``, ``empty_scope``).

    A service that keeps rate-limiting the run stops it cleanly: what was yielded
    stays, the state is not advanced, and the next run resumes. An access
    failure raises.
    """
    stats.clear()
    stats.update(
        items_selected=0,
        items_changed=0,
        items_unchanged=0,
        deleted=0,
        redirected=0,
        truncated=0,
        requests=0,
        full_sync=0,
        failed=0,
    )
    try:
        yield from _sync(client, settings, state, stats)
    except WikidataRateLimitedError as exc:
        stats["failed"] = 1
        stats["failed_rate_limit"] = 1
        logger.warning("Wikidata: rate limited (%s); the next run resumes from here.", exc.code)
    except WikidataAuthError:
        stats["failed"] = 1
        stats["failed_auth"] = 1
        raise
    finally:
        stats["requests"] = client.requests


def _entity_url(api_url: str, entity: dict) -> str:
    parsed = urlparse(api_url)
    page = entity.get("title") or entity["id"]
    return f"{parsed.scheme}://{parsed.netloc}/wiki/{page}"


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def _default_resource_name(api_url: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", urlparse(api_url).netloc.lower()).strip("_")
    return f"wikidata_{slug}"


def wikidata_source(
    items: list[str] | None = None,
    *,
    expand_properties: list[str] | None = None,
    expand_depth: int = 1,
    sparql: str | None = None,
    sparql_variable: str = "item",
    max_items: int = 500,
    language: str = "en",
    include_external_ids: bool = False,
    sitelinks: list[str] | None = None,
    full_sync: bool = False,
    api_url: str = DEFAULT_API_URL,
    sparql_url: str | None = None,
    user_agent: str = DEFAULT_USER_AGENT,
    maxlag: int | None = 5,
    resource_name: str | None = None,
    check_active: Callable[[], None] | None = None,
    service: Any = None,
    http_client: Any = None,
):
    """Return a ``dlt`` resource yielding one document row per selected item.

    Hand the result to ``cognee.remember(...)`` with
    ``write_disposition="merge"``, ``primary_key="id"`` and
    ``max_rows_per_table=0``. Configuration is passed explicitly; reading it
    from the environment is left to the caller (see ``examples/example.py``).

    Args:
        items: Seed item or property ids (``"Q42"``), entity URIs or item page
            URLs. A seed that does not exist (deleted or never assigned) raises
            ``WikidataScopeError`` when it is first configured.
        expand_properties: Property ids to follow from each selected item
            (e.g. ``["P800"]``, notable work). Their item values join the
            selection, up to ``expand_depth`` hops from the seeds.
        expand_depth: How many hops to follow ``expand_properties``.
        sparql: A SPARQL query whose ``?item`` (see ``sparql_variable``)
            bindings join the selection. Keep it to a ``LIMIT``-ed query: the
            Wikidata Query Service stops queries after 60 seconds.
        sparql_variable: The query variable holding the entity URIs.
        max_items: Upper bound on the selection (seeds, query results and
            expansion together).
        language: Language of labels, descriptions and aliases. Labels fall
            back through the language chain (including ``mul``).
        include_external_ids: Also render external-identifier statements
            (catalogue ids such as VIAF or ISNI). Off by default: they are most
            of an item's statements and carry little meaning for retrieval.
        sitelinks: Site ids whose links are kept (default: the ``language``
            Wikipedia, e.g. ``enwiki``); ``[]`` keeps none.
        full_sync: Re-fetch and re-render every selected item on this run.
        api_url: The Wikibase ``api.php`` URL (default: Wikidata's).
        sparql_url: SPARQL endpoint for ``sparql``. Defaults to the Wikidata
            Query Service when ``api_url`` is Wikidata's; required otherwise.
        user_agent: User-Agent header. Wikimedia asks for contact details, e.g.
            ``"my-app/1.0 (me@example.com)"``.
        maxlag: The ``maxlag`` seconds sent with Action API requests (``None``
            to omit it); "too busy" answers are retried after the
            ``Retry-After`` sent, up to 60 seconds.
        resource_name: dlt resource / staging table name, also the key of the
            incremental state. Defaults to one derived from ``api_url``. Two
            sources sharing a name share state, so each would forget the
            other's items.
        check_active: Optional host authorization checkpoint during extraction.
        service: Pre-built ``httpx.Client`` (mainly a test injection point).
        http_client: Alias of ``service``.
    """
    import dlt
    import httpx

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 2:
        raise RuntimeError(
            "The Wikidata connector requires a cognee build that reads per-row node "
            "sets (DOCUMENT_SYNC_VERSION >= 2, cognee 1.6.3+). Upgrade cognee."
        )

    if service is not None and http_client is not None and service is not http_client:
        raise ValueError("Pass the pre-built client as service= or http_client=, not both.")
    injected_client = service if service is not None else http_client

    resolved_api_url = (api_url or "").strip()
    parsed = urlparse(resolved_api_url)
    if parsed.scheme not in ("http", "https") or not parsed.path.endswith("api.php"):
        raise ValueError(
            "wikidata_source needs the Wikibase api.php URL, e.g. "
            "https://www.wikidata.org/w/api.php."
        )
    if not user_agent or not user_agent.strip():
        raise ValueError("user_agent must not be empty.")
    seeds = tuple(dict.fromkeys(normalize_entity_id(item) for item in items or []))
    if not seeds and not sparql:
        raise ValueError("Select something to sync: pass items=[...] and/or sparql=...")
    properties = tuple(dict.fromkeys(normalize_entity_id(p) for p in expand_properties or []))
    if any(not prop.startswith("P") for prop in properties):
        raise ValueError("expand_properties takes property ids such as 'P800'.")
    if expand_depth < 0 or max_items < 1:
        raise ValueError("expand_depth must not be negative and max_items must be positive.")
    if not re.fullmatch(r"[a-z]{2,3}(-[a-z0-9-]+)?", language):
        raise ValueError(f"language must be a language code such as 'en', got {language!r}.")

    resolved_sparql_url = sparql_url
    if not resolved_sparql_url and parsed.netloc == urlparse(DEFAULT_API_URL).netloc:
        resolved_sparql_url = DEFAULT_SPARQL_URL
    if sparql and not resolved_sparql_url:
        raise ValueError("sparql= needs sparql_url= for this Wikibase.")

    settings = _Settings(
        items=seeds,
        expand_properties=properties,
        expand_depth=expand_depth,
        sparql=sparql.strip() if sparql else None,
        sparql_variable=sparql_variable.lstrip("?"),
        max_items=max_items,
        language=language,
        include_external_ids=include_external_ids,
        sitelinks=tuple(sitelinks if sitelinks is not None else [f"{language}wiki"]),
        full_sync=full_sync,
    )
    name = resource_name or _default_resource_name(resolved_api_url)
    stats: dict[str, int] = {}

    @dlt.resource(
        name=name,
        primary_key="id",
        write_disposition="merge",
        # _deleted is a boolean hard-delete marker: rows where it is True are
        # removed from the dlt destination on merge, which propagates the
        # deletion through cognee's orphan_cleanup. cognee_node_set needs no
        # hint here: ingest_dlt_source applies it to every document source.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def wikidata_items():
        # The query service can take up to its 60 s limit to answer.
        owned_client = (
            None
            if injected_client is not None
            else httpx.Client(timeout=75.0, follow_redirects=True)
        )
        try:
            client = _WikibaseClient(
                injected_client or owned_client,
                resolved_api_url,
                user_agent,
                maxlag,
                sparql_url=resolved_sparql_url,
            )
            state = dlt.current.resource_state()
            if state.get("items") and state.get("api_url") not in (None, resolved_api_url):
                # Same resource name, different Wikibase: syncing on would
                # tombstone every item of the other one.
                raise ValueError(
                    f"Wikidata resource {name!r} already holds items from another Wikibase "
                    "in this dataset. Give each one its own resource_name."
                )
            state["api_url"] = resolved_api_url
            yield from dlt_utils.guarded_rows(
                sync_items(client, settings, state, stats), check_active
            )
        finally:
            if owned_client is not None:
                owned_client.close()

    resource = wikidata_items()
    # Opt into the document ingestion path: each item row (id/title/content/url
    # plus cognee_node_set) becomes a text document that flows through normal
    # cognify. resolve_dlt_sources reads this marker; it never imports this
    # connector.
    setattr(resource, DOCUMENT_SOURCE_ATTR, SOURCE_TAG)
    # Keep the incremental state per (dataset, resource), so syncing the same
    # selection into another dataset does not reuse this one's state.
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, name)
    # Host-readable diagnostics: int counters only, never item content.
    resource.cognee_sync_stats = stats
    return resource
