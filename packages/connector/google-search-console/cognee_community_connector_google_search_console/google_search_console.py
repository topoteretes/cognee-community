"""DLT source for Google Search Console (monthly performance documents + forget-on-delete).

Syncs how the selected Search Console properties perform in Google Search into cognee::

    import cognee
    from cognee_community_connector_google_search_console import google_search_console_source

    await cognee.remember(
        google_search_console_source(sites=["sc-domain:example.com"]),
        dataset_name="search_console",
        primary_key="id",
        write_disposition="merge",  # required: the source emits deletions
    )

Design
------
* **Documents** — Search Analytics is a table of numbers, not prose, and one row per
  date/query/page would be millions of documents. So each calendar month of a property becomes an
  *overview* (totals, devices, top countries, top queries, top pages) and one document per top
  page with the queries that led to it. They share a node set (``search-console:<property>``), so
  cognify links them in the graph, and the same query or page in two months is one entity.
* **Incremental sync** — only final data is read (``dataState="final"``), which Search Console
  publishes about two days late. A month is re-read on every run until it is a few days older than
  that, because Google can still revise the last days; after that it is closed and never read
  again. Only documents whose text changed are sent on, so an unchanged run makes no LLM call.
* **Forget-on-delete** — a property that is removed, loses its verification, or is taken out of
  the selection is forgotten whole: its documents become ``_deleted`` tombstones, which dlt drops
  and cognee's orphan cleanup removes from the graph and vector stores. A page that falls out of
  a still-open month's top pages is forgotten with it.
* **Errors and sampling never delete** — a failed request aborts the run before dlt commits rows
  or state. Google returns only the top rows by clicks, so a query that is absent is not a deleted
  one: closed months are never re-read, and a month that suddenly returns no data keeps its
  documents. Months that leave the ``months`` window stay in memory.
"""

import hashlib
import re
from collections import defaultdict
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import (
    DOCUMENT_SOURCE_ATTR,
    NODE_SET_COLUMN,
    NODE_SET_COLUMN_HINT,
    PIPELINE_SCOPE_ATTR,
    guarded_rows,
)

from .auth import GoogleSearchConsoleSettings, resolve_credentials
from .client import ROW_LIMIT, SearchConsoleAPIError, SearchConsoleClient
from .render import PeriodInfo, overview_content, page_content, period_label, site_label

logger = get_logger("google_search_console_connector")

SOURCE_NAME = "google_search_console"
DEFAULT_RESOURCE_NAME = "google_search_console"

# Bump when rendering or the state layout changes, so the next run re-reads every month
# instead of keeping documents rendered by an older connector.
_STATE_VERSION = 1

# Search Console publishes final data about two days late. A month stays open for a few more
# days, in case Google revises its last days after publishing them.
DATA_LAG_DAYS = 2
LOOKBACK_DAYS = 3
# Search Console keeps 16 months of data.
MAX_MONTHS = 16
SEARCH_TYPES = ("web", "image", "video", "news", "discover", "googleNews")
# Rows read per month to find the queries behind the top pages (the top ones by clicks).
MAX_ROWS_PER_PERIOD = 50_000
TOP_COUNTRIES = 5


@dataclass(frozen=True)
class _Selection:
    """What to sync, normalized from the arguments of ``google_search_console_source``."""

    sites: tuple[str, ...] | None
    months: int
    search_type: str
    top_queries: int
    top_pages: int
    queries_per_page: int
    full_refresh: bool
    resource_name: str

    @property
    def fingerprint(self) -> str:
        """Changes when an option that shapes a month's documents changes."""
        shape = (self.search_type, self.top_queries, self.top_pages, self.queries_per_page)
        return hashlib.sha256(repr(shape).encode()).hexdigest()[:12]


@dataclass
class _Run:
    """The inputs and outputs shared by every step of one sync."""

    client: Any
    selection: _Selection
    today: date
    trusted: bool
    tombstones: list[tuple[str, str]]
    stats: dict
    check_active: Callable[[], None] | None = None

    def api(self) -> "_GuardedClient":
        return _GuardedClient(self.client, self.check_active)


class _GuardedClient:
    """The client, asking ``check_active`` before every request.

    ``guarded_rows`` only checks between rows, but a month is read in several requests before
    its first row, so a revoked connection could not stop a sync in the middle of a read.
    """

    def __init__(self, client: Any, check_active: Callable[[], None] | None):
        self._client = client
        self._check = check_active

    def __getattr__(self, name: str):
        method = getattr(self._client, name)

        def call(*args, **kwargs):
            if self._check is not None:
                self._check()
            return method(*args, **kwargs)

        return call


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def google_search_console_source(
    sites: Sequence[str] | str | None = None,
    *,
    months: int = 3,
    search_type: str = "web",
    top_queries: int = 25,
    top_pages: int = 25,
    queries_per_page: int = 10,
    full_refresh: bool = False,
    resource_name: str = DEFAULT_RESOURCE_NAME,
    access_token: str | None = None,
    settings: GoogleSearchConsoleSettings | None = None,
    client: Any = None,
    service: Any = None,
    check_active: Callable[[], None] | None = None,
    today: date | None = None,
):
    """Create a dlt source that yields Search Console performance as documents.

    Args:
        sites: Properties to sync, exactly as ``list_properties`` shows them
            (``sc-domain:example.com`` or ``https://example.com/``). Defaults to every verified
            property the account can see.
        months: Calendar months to sync, counting the current one (1 to 16). Months that later
            leave this window stay in memory.
        search_type: ``web`` (default), ``image``, ``video``, ``news``, ``discover`` or
            ``googleNews``. Give each search type its own ``resource_name``.
        top_queries: Queries listed in a month's overview.
        top_pages: Pages per month that get a document (0 for none).
        queries_per_page: Queries listed in a page document (0 for none).
        full_refresh: Read every month in the window again, for example after changing how
            documents are rendered.
        resource_name: Stable name for this connection's dlt state and staging tables. Give each
            Google account its own name when several sync into one dataset.
        access_token: A ready-made token from a host. It is never renewed. Without it, the
            credentials come from ``settings``.
        settings: Google OAuth settings; read from ``GOOGLE_SEARCH_CONSOLE_*`` when omitted.
        client: A ``SearchConsoleClient`` (mainly a test-injection point).
        service: An alias for ``client``, like the other connectors.
        check_active: Called before every request and between rows; raises to stop a sync whose
            access was revoked mid-run (for hosts).
        today: Pin the date the window is counted from, mainly for tests.

    Returns:
        A dlt source for ``cognee.remember(...)``. Pass ``write_disposition="merge"``:
        incremental runs emit only changes and tombstones, and any other disposition is refused.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 2:
        raise RuntimeError(
            "The Google Search Console connector requires a cognee build with table-scoped "
            "document cleanup and per-row node sets (DOCUMENT_SYNC_VERSION 2, cognee 1.6.4)."
        )
    if not re.fullmatch(r"[a-z][a-z0-9_]*", resource_name):
        raise ValueError("resource_name must be lower case letters, digits and underscores.")
    if isinstance(sites, str):
        sites = [sites]
    if sites is not None and not [site for site in sites if site.strip()]:
        # An empty selection would forget everything synced before.
        raise ValueError("sites is empty; omit it to sync every verified property.")
    if not 1 <= months <= MAX_MONTHS:
        raise ValueError(f"months must be between 1 and {MAX_MONTHS}.")
    if search_type not in SEARCH_TYPES:
        raise ValueError(f"search_type must be one of {', '.join(SEARCH_TYPES)}.")
    if min(top_queries, top_pages, queries_per_page) < 0:
        raise ValueError("top_queries, top_pages and queries_per_page cannot be negative.")
    if max(top_queries, top_pages) > ROW_LIMIT:
        # Google rejects a query for more rows than this.
        raise ValueError(f"top_queries and top_pages are capped at {ROW_LIMIT:,}.")

    selection = _Selection(
        sites=tuple(dict.fromkeys(site.strip() for site in sites)) if sites else None,
        months=months,
        search_type=search_type,
        top_queries=top_queries,
        top_pages=top_pages,
        queries_per_page=queries_per_page,
        full_refresh=full_refresh,
        resource_name=resource_name,
    )
    # Fail when the source is built, not halfway through a pipeline run.
    client = client or service or SearchConsoleClient(resolve_credentials(access_token, settings))
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        # Rows with _deleted=True are removed from the staging table on merge,
        # which is what makes cognee's orphan cleanup forget them.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}, **NODE_SET_COLUMN_HINT},
    )
    def search_console_rows():
        # The caller's write_disposition overrides the one declared above. A replace run of an
        # incremental read would empty every table but the changed rows, so refuse it before
        # reading anything.
        disposition = source.resources[resource_name].write_disposition
        if disposition != "merge":
            raise ValueError(
                f'google_search_console_source needs write_disposition="merge", got '
                f"{disposition!r}. Its incremental runs emit only changes and deletions."
            )
        try:
            current = today or datetime.now(timezone.utc).date()
            rows = sync_rows(
                client,
                selection,
                dlt.current.resource_state(),
                current,
                stats=stats,
                check_active=check_active,
            )
            for table_name, row in guarded_rows(rows, check_active):
                yield dlt.mark.with_table_name(row, table_name)
        finally:
            client.close()

    @dlt.source(name=SOURCE_NAME)
    def _search_console():
        return search_console_rows

    source = _search_console()
    # Opt into the document path (period -> text document -> cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(source, DOCUMENT_SOURCE_ATTR, SOURCE_NAME)
    # Give the source its own dlt pipeline per dataset, so its state is not shared with
    # unrelated dlt sources.
    setattr(source, PIPELINE_SCOPE_ATTR, resource_name)
    # Counts of the last run, for hosts that record how a sync went. Never content.
    source.cognee_sync_stats = stats
    return source


def list_properties(
    access_token: str | None = None,
    *,
    settings: GoogleSearchConsoleSettings | None = None,
    client: Any = None,
) -> list[dict]:
    """The properties the account can see, each ``{siteUrl, permissionLevel}``.

    Pass one of the ``siteUrl`` values as ``sites=`` to select it. A property whose permission
    level is ``siteUnverifiedUser`` cannot be read and is never synced.
    """
    client = client or SearchConsoleClient(resolve_credentials(access_token, settings))
    try:
        return client.list_sites()
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Sync (pure given a client + state dict — unit-testable)
# ---------------------------------------------------------------------------
def sync_rows(
    client: Any,
    selection: _Selection,
    state: dict,
    today: date,
    *,
    stats: dict | None = None,
    check_active: Callable[[], None] | None = None,
) -> Iterator[tuple[str, dict]]:
    """Yield ``(staging_table, row)`` for every changed document and tombstone.

    ``state`` stands in for dlt's resource state. It is only written after the last request
    has succeeded, and dlt only commits it together with the rows, so a failed run changes
    nothing. The one exception is a rate limit, which ends the run cleanly: the months read so
    far are committed and the next run resumes with the rest.
    """
    if stats is None:
        stats = {}
    stats.clear()
    stats.update(
        sites=0,
        periods=0,
        closed=0,
        documents=0,
        unchanged=0,
        deleted=0,
        kept=0,
        failed_rate_limit=0,
    )

    # A state written by another rendering is kept for its ids (so deletions still work) but its
    # digests are not trusted.
    trusted = state.get("version") == _STATE_VERSION and not selection.full_refresh
    run = _Run(client, selection, today, trusted, [], stats, check_active)
    api = run.api()
    previous: dict[str, dict] = state.get("sites", {})
    current: dict[str, dict] = {}
    sites = _resolve_sites(api, selection, previous)

    stopped = False
    for site in sites:
        before = previous.get(site["url"], {}).get("periods", {})
        # Months that left the window, and months not reached after a stop, stay as they were.
        periods = dict(before)
        for period, first, last in [] if stopped else _months(today, selection.months):
            try:
                entry = yield from _sync_period(run, site, period, first, last, before.get(period))
            except SearchConsoleAPIError as exc:
                if not exc.is_rate_limit:
                    raise
                # Everything read before this month is kept; the rest waits for the next run.
                stopped = True
                stats["failed_rate_limit"] = 1
                logger.warning("Search Console: rate limited, stopping; the next run resumes.")
                break
            if entry is not None:
                periods[period] = entry
        current[site["url"]] = {"table": site["table"], "periods": periods}
        stats["sites"] += 1

    # Properties removed upstream or from the selection: all of their documents are out of scope.
    chosen = {site["url"] for site in sites}
    for url, entry in previous.items():
        if url not in chosen:
            for period in entry.get("periods", {}).values():
                for doc_id in period.get("docs", {}):
                    _forget(run, entry["table"], doc_id)

    for table, row_id in run.tombstones:
        yield table, {"id": row_id, "_deleted": True}

    state["version"] = _STATE_VERSION
    state["sites"] = current
    logger.info("Search Console: sync stats %s", stats)


def _sync_period(run: _Run, site: dict, period: str, first: date, last: date, prior: dict | None):
    """Sync one month of one property. Yields changed documents and returns its new entry."""
    selection, stats = run.selection, run.stats
    # Only final data exists, so a month ends where Google has published it.
    end = min(last, run.today - timedelta(days=DATA_LAG_DAYS))
    if end < first:
        return None
    fingerprint = selection.fingerprint
    if run.trusted and prior and prior.get("closed") and prior.get("fp") == fingerprint:
        stats["closed"] += 1
        return prior

    info = PeriodInfo(site["url"], period, first, end, selection.search_type)
    data = _read_period(run.api(), selection, info)
    known: dict[str, str] = (prior or {}).get("docs", {})
    if data is None and known:
        # Google returned nothing for a month that had data. That is far more likely a gap on
        # their side than every click vanishing, so keep what is in memory.
        logger.warning("Search Console: a month with documents returned no data; keeping it.")
        run.stats["kept"] += 1
        return prior

    digests: dict[str, str] = {}
    for doc_id, title, content in _documents(selection, info, data) if data else []:
        digests[doc_id] = hashlib.sha256(f"{title}\n{content}".encode()).hexdigest()[:16]
        if run.trusted and known.get(doc_id) == digests[doc_id]:
            stats["unchanged"] += 1
            continue
        stats["documents"] += 1
        yield (
            site["table"],
            {
                "id": doc_id,
                "title": title,
                "content": content,
                NODE_SET_COLUMN: [site["nodeSet"]],
                "_deleted": False,
            },
        )
    # A page that fell out of this month's top pages is no longer part of its view.
    for doc_id in sorted(set(known) - set(digests)):
        _forget(run, site["table"], doc_id)
    stats["periods"] += 1
    settled = run.today - timedelta(days=DATA_LAG_DAYS + LOOKBACK_DAYS)
    return {"closed": last <= settled, "fp": fingerprint, "docs": digests}


def _read_period(client, selection: _Selection, info: PeriodInfo) -> dict | None:
    """Read what one month's documents are built from, or None when it has no data."""
    site_url = info.site_url
    base = {
        "startDate": info.first.isoformat(),
        "endDate": info.end.isoformat(),
        "type": selection.search_type,
        "dataState": "final",
    }

    def query(dimensions: list[str], limit: int | None = None) -> list[dict]:
        body: dict[str, Any] = {**base, "dimensions": dimensions}
        if limit is not None:
            body["rowLimit"] = limit
        return client.query(site_url, body)

    totals = client.query(site_url, base)
    # With no data Google still answers a dimensionless query with one row of zeros.
    if not totals or not totals[0].get("impressions"):
        return None
    pages = query(["page"], selection.top_pages) if selection.top_pages else []
    page_queries: dict[str, list[dict]] = defaultdict(list)
    if pages and selection.queries_per_page:
        body = {**base, "dimensions": ["page", "query"]}
        for row in client.query_all(site_url, body, MAX_ROWS_PER_PERIOD):
            page_queries[row["keys"][0]].append(row)
    return {
        "totals": totals[0],
        "devices": query(["device"]),
        "countries": query(["country"], TOP_COUNTRIES),
        "queries": query(["query"], selection.top_queries) if selection.top_queries else [],
        "pages": pages,
        "page_queries": page_queries,
    }


def _documents(selection: _Selection, info: PeriodInfo, data: dict) -> list[tuple[str, str, str]]:
    """The ``(id, title, content)`` of a month: an overview and a document per top page."""
    name, site_url = selection.resource_name, info.site_url
    label, month = site_label(site_url), period_label(info.period)
    docs = [
        (
            _overview_id(name, site_url, info.period),
            f"Search performance of {label}, {month}",
            overview_content(info, data),
        )
    ]
    for page in data["pages"]:
        url = page["keys"][0]
        queries = data["page_queries"].get(url, [])[: selection.queries_per_page]
        docs.append(
            (
                _page_id(name, site_url, info.period, url),
                f"Search performance of {url}, {month}",
                page_content(info, page, queries),
            )
        )
    return docs


def _forget(run: _Run, table: str, row_id: str) -> None:
    run.stats["deleted"] += 1
    run.tombstones.append((table, row_id))


# ---------------------------------------------------------------------------
# Properties and months
# ---------------------------------------------------------------------------
def _resolve_sites(client, selection: _Selection, previous: dict[str, dict]) -> list[dict]:
    listing = client.list_sites()
    # An unverified property is listed but refuses every data request.
    readable = {
        entry["siteUrl"]
        for entry in listing
        if entry.get("siteUrl") and entry.get("permissionLevel") != "siteUnverifiedUser"
    }
    if not readable:
        # An empty selection would forget everything synced before.
        raise ValueError(
            "This Google account can see no verified Search Console property. Check that you "
            "signed in with the right account and that the property is verified."
        )
    if selection.sites is None:
        chosen = sorted(readable)
        for url in previous:
            # The listing is the only sign of a removed property, so ask for it by name before
            # forgetting it.
            if url not in readable and _still_readable(client, url):
                chosen.append(url)
    else:
        missing = [url for url in selection.sites if url not in readable]
        if missing:
            # Skipping it would forget what it synced before; make the user decide.
            raise ValueError(
                f"Search Console property {missing[0]!r} was not found, or is not verified for "
                f"this account. Available: {sorted(readable)}. Remove it from `sites` to forget "
                "its content."
            )
        chosen = list(selection.sites)
    name = selection.resource_name
    return [
        {
            "url": url,
            "table": _table(name, url),
            "nodeSet": f"search-console:{url}",
        }
        for url in chosen
    ]


def _still_readable(client, site_url: str) -> bool:
    try:
        entry = client.get_site(site_url)
    except SearchConsoleAPIError as exc:
        if exc.status in (403, 404):
            return False
        raise
    return entry.get("permissionLevel") != "siteUnverifiedUser"


def _months(today: date, count: int) -> list[tuple[str, date, date]]:
    """``(YYYY-MM, first day, last day)`` of the last ``count`` months, oldest first."""
    year, month = today.year, today.month
    months = []
    for _ in range(count):
        first = date(year, month, 1)
        following = date(year + month // 12, month % 12 + 1, 1)
        months.append((f"{year:04d}-{month:02d}", first, following - timedelta(days=1)))
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return months[::-1]


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
def _overview_id(name: str, site_url: str, period: str) -> str:
    return f"{name}:overview:{site_url}:{period}"


def _page_id(name: str, site_url: str, period: str, page_url: str) -> str:
    digest = hashlib.sha256(page_url.encode()).hexdigest()[:16]
    return f"{name}:page:{site_url}:{period}:{digest}"


def _table(name: str, site_url: str) -> str:
    """One staging table per property, so cleanup stays scoped to it."""
    slug = re.sub(r"[^a-z0-9]+", "_", site_label(site_url).lower()).strip("_")[:30]
    # Two properties can slug alike (http vs https, a trailing slash), so the hash tells them
    # apart.
    return f"{name}_{slug}_{hashlib.sha256(site_url.encode()).hexdigest()[:8]}"
