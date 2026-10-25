"""Coda connector for cognee: a ``dlt`` source that turns Coda docs into memory.

Sync the pages and tables of Coda docs into cognee, incrementally and with
forget-on-delete. The resource is handed directly to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_coda import coda_source

    await cognee.remember(
        coda_source(doc_ids=["<doc id>"], token=token),
        dataset_name="coda",
        primary_key="id",
        write_disposition="merge",  # REQUIRED, the add pipeline defaults to "replace"
        max_rows_per_table=0,
    )

Design
------
* **Rows** are flat ``{id, title, content, url, _deleted}``: one per page
  (``page:<doc id>:<page id>``, the page exported as markdown) and one per table
  (``table:<doc id>:<table id>``, every row rendered as a labeled block). A table
  is one document rather than a node per row: rows keep each other's context and
  a changed table costs one cognify, not one per row. Views are skipped, since
  they show the rows of a table that is already synced.
* **Incremental.** The API has no ``changedSince`` filter, so every run lists the
  pages and tables of each doc (cheap) and compares their ``updatedAt`` with the
  last run; a table's ``updatedAt`` comes from its own endpoint, since the listing
  has none. Only changed pages are exported again and only changed tables have
  their rows read again.
* **Forget-on-delete.** A page or table missing from its doc's listing, a doc
  that answers 404 or 410, and a doc that leaves the listing or the selection
  are emitted as hard-delete tombstones. A 403 (access removed) is never read as
  deletion: that doc or page is left as it is.
* **A run is bounded.** A long rate limit, a spent retry wait budget or a spent
  request budget stops the run cleanly, keeps the docs it finished and reports
  why in ``cognee_sync_stats``. Any other failure raises, and dlt rolls the state
  of a failed run back.

Privacy
-------
This reads the content of every selected doc. Nothing is fetched until you
construct a source and call ``remember``, and anyone with read access to the
target dataset can read what was ingested.
"""

import copy
import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("coda_connector")

DEFAULT_BASE_URL = "https://coda.io/apis/v1"
# Coda usually answers in well under a second; only a stuck connection hits this.
_TIMEOUT = 30.0
# Attempts per request; backing off 1, 2, 4 and 8 seconds gives up after about 15.
_RETRIES = 5
# Coda's read limits reset within seconds; a longer Retry-After stops the run.
_MAX_RETRY_SLEEP = 60.0
# Total time one run may wait on retries before it stops cleanly.
DEFAULT_MAX_SLEEP = 300.0
# Requests per run; a page export counts as one. Bigger accounts finish over several runs.
DEFAULT_MAX_REQUESTS = 1000
# Page exports run in the background; a large page finishes well within two minutes.
_EXPORT_TIMEOUT = 120.0
# Largest ``limit`` the columns endpoint accepts.
_COLUMNS_PAGE = 100
# Views show rows of a base table that is synced already.
_TABLE_TYPES = "table,database"
# Not found and gone: the doc or page was deleted. A 403 only means access was removed.
_DELETED = (404, 410)
_FORBIDDEN = 403
_STATS = (
    "emitted",
    "unchanged",
    "exported",
    "deleted",
    "gone",
    "unreadable",
    "failed",
)


# ---------------------------------------------------------------------------
# Errors: messages carry the HTTP status only, never the token, links or bodies.
# ---------------------------------------------------------------------------
class CodaSourceError(RuntimeError):
    """Base class of the errors this source raises."""


class CodaAuthError(CodaSourceError):
    """Coda rejected the API token (HTTP 401)."""


class CodaRateLimitedError(CodaSourceError):
    """Coda kept answering HTTP 429, asked for a long wait, or the wait budget is spent."""


class CodaAPIError(CodaSourceError):
    """Coda answered with an error, or could not be reached."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
def _retry_after(response: Any) -> float | None:
    try:
        return max(float(response.headers.get("Retry-After")), 0.0)
    except (TypeError, ValueError):
        return None


class CodaClient:
    """Minimal synchronous client for the Coda API.

    ``get`` returns the decoded JSON body and ``export_markdown`` the markdown of
    one page. Tests and hosts can pass any object with the same two methods to
    :func:`coda_source` instead.
    """

    def __init__(
        self,
        token: str,
        base_url: str = DEFAULT_BASE_URL,
        *,
        http: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        max_sleep: float = DEFAULT_MAX_SLEEP,
    ):
        token = (token or "").strip()
        # Reject early and without echoing the value: httpx would put an invalid
        # header value, token included, into its exception.
        if not token or not token.isascii() or not token.isprintable() or " " in token:
            raise ValueError("The Coda API token is empty or has an invalid format")
        import httpx

        self.base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._http = http or httpx.Client(timeout=_TIMEOUT, follow_redirects=True)
        self._sleep = sleep
        self._clock = clock
        self._max_sleep = max_sleep
        self._slept = 0.0

    def __repr__(self) -> str:
        return "CodaClient(token=<redacted>)"

    def reset_sleep_budget(self) -> None:
        """Start a new run's retry wait budget."""
        self._slept = 0.0

    def _wait(self, delay: float) -> None:
        if self._slept + delay > self._max_sleep:
            raise CodaRateLimitedError("Coda retries used up the wait budget of this run")
        self._slept += delay
        self._sleep(delay)

    def _send(self, method: str, url: str, headers: dict[str, str], **kwargs: Any) -> Any:
        import httpx

        attempt = 0
        while True:
            last = attempt == _RETRIES - 1
            try:
                response = self._http.request(method, url, headers=headers, **kwargs)
            except httpx.TransportError as exc:
                if last:
                    # Not chained: the export download URL is signed, and httpx puts
                    # the URL into its exception.
                    raise CodaAPIError(f"Coda request failed: {type(exc).__name__}") from None
                self._wait(float(2**attempt))
                attempt += 1
                continue
            status = response.status_code
            if status == 429:
                wait = _retry_after(response)
                if last or (wait is not None and wait > _MAX_RETRY_SLEEP):
                    raise CodaRateLimitedError("Coda rate limit exceeded (HTTP 429)")
                self._wait(wait if wait is not None else float(2**attempt))
            elif status >= 500 and not last:
                wait = _retry_after(response)
                self._wait(min(wait if wait is not None else float(2**attempt), _MAX_RETRY_SLEEP))
            elif status == 401:
                raise CodaAuthError("Coda rejected the API token (HTTP 401)")
            elif status not in (200, 202):
                raise CodaAPIError(f"Coda request failed: HTTP {status}", status=status)
            else:
                return response
            attempt += 1

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._send("GET", self.base_url + path, self._headers, params=params).json()

    def export_markdown(self, doc_id: str, page_id: str) -> str:
        """Start a markdown export of one page, wait for it, and download it."""
        path = f"{self.base_url}/docs/{doc_id}/pages/{page_id}/export"
        started = self._send("POST", path, self._headers, json={"outputFormat": "markdown"})
        request_id = started.json()["id"]
        deadline = self._clock() + _EXPORT_TIMEOUT
        delay = 1.0
        while True:
            status = self._send("GET", f"{path}/{request_id}", self._headers).json()
            if status.get("status") == "complete":
                # The download link is a short-lived signed URL: no token is sent.
                return self._send("GET", status["downloadLink"], {}).text
            if status.get("status") == "failed":
                raise CodaAPIError("Coda page export failed")
            if self._clock() >= deadline:
                raise CodaAPIError("Coda page export timed out")
            self._sleep(delay)
            delay = min(delay * 2, 5.0)


# ---------------------------------------------------------------------------
# Rendering: deterministic, no volatile fields.
# ---------------------------------------------------------------------------
def _cell(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(text for text in (_cell(v) for v in value) if text)
    if value is None or isinstance(value, dict):
        return ""
    return str(value).strip()


def render_page(doc: dict, page: dict, markdown: str) -> dict[str, Any]:
    name = str(page.get("name") or "").strip() or "Untitled page"
    content = f"Doc: {doc.get('name') or doc['id']}\n\n{markdown.strip()}".strip()
    return {
        "id": f"page:{doc['id']}:{page['id']}",
        "title": name,
        "content": content,
        "url": str(page.get("browserLink") or ""),
        "_deleted": False,
    }


def render_table(doc: dict, table: dict, columns: list[dict], rows: list[dict]) -> dict[str, Any]:
    display = ((table.get("displayColumn") or {}).get("id")) or ""
    parts = [f"Doc: {doc.get('name') or doc['id']}", f"Table: {table.get('name') or table['id']}"]
    for row in sorted(rows, key=lambda r: (r.get("index") or 0, str(r.get("id")))):
        values = row.get("values") or {}
        parts.extend(["", f"## Row: {_cell(row.get('name')) or row.get('id')}"])
        for column in columns:
            text = _cell(values.get(column["id"]))
            if text and column["id"] != display:
                parts.append(f"- {column.get('name') or column['id']}: {text}")
    return {
        "id": f"table:{doc['id']}:{table['id']}",
        "title": str(table.get("name") or table["id"]),
        "content": "\n".join(parts),
        "url": str(table.get("browserLink") or ""),
        "_deleted": False,
    }


def _digest(row: dict) -> str:
    return hashlib.md5(json.dumps(row, sort_keys=True).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _CodaConfig:
    doc_ids: tuple[str, ...]
    workspace_id: str | None
    folder_id: str | None
    include_tables: bool
    include_hidden_pages: bool
    include_sync_pages: bool
    max_requests: int = DEFAULT_MAX_REQUESTS


class _CutShortError(Exception):
    """Internal: stop the run here, keeping the docs finished so far."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _wanted_page(page: dict, config: _CodaConfig) -> bool:
    kind = page.get("contentType") or "canvas"
    if kind == "embed" or (kind == "syncPage" and not config.include_sync_pages):
        return False
    return config.include_hidden_pages or not page.get("isEffectivelyHidden")


def _forget(doc_id: str, entry: dict, stats: dict[str, int]) -> Iterator[dict]:
    for kind, prefix in (("pages", "page"), ("tables", "table")):
        for item_id in entry.get(kind) or {}:
            stats["deleted"] += 1
            yield {"id": f"{prefix}:{doc_id}:{item_id}", "_deleted": True}


class _Walker:
    """One run over the selected docs, writing progress into ``state``."""

    def __init__(
        self,
        client: Any,
        config: _CodaConfig,
        state: dict,
        stats: dict[str, int],
        check_active: Callable[[], None] | None,
    ):
        self.client = client
        self.config = config
        self.state = state
        self.stats = stats
        self.check_active = check_active
        self.known: dict[str, dict] = state.setdefault("docs", {})
        self.requests = 0
        # Set once a doc is finished; the budget never stops a run before that.
        self.progressed = False

    def _call(self, method: Callable[..., Any], *args: Any) -> Any:
        if self.check_active is not None:
            self.check_active()
        if self.progressed and self.requests >= self.config.max_requests:
            raise _CutShortError("budget")
        self.requests += 1
        try:
            return method(*args)
        except CodaRateLimitedError:
            raise _CutShortError("rate_limit") from None

    def _items(self, path: str, params: dict[str, Any] | None = None) -> Iterator[dict]:
        """Yield every item of a paginated Coda list."""
        token = None
        while True:
            query = {**(params or {}), **({"pageToken": token} if token else {})}
            page = self._call(self.client.get, path, query)
            items = page.get("items")
            # A 200 without the list must not read as "nothing exists".
            if not isinstance(items, list):
                raise CodaAPIError("Coda answered without an items list")
            yield from items
            next_token = page.get("nextPageToken")
            if not next_token:
                return
            if next_token == token:
                raise CodaAPIError("Coda pagination did not advance")
            token = next_token

    def _unreadable(self, doc_id: str) -> None:
        logger.warning("Coda: doc %s answered 403, left unchanged.", doc_id)
        self.stats["unreadable"] += 1

    # -- the doc list ------------------------------------------------------
    def _docs(self) -> tuple[list[dict], set[str]]:
        """The docs to sync, and the selected ones the token can no longer read."""
        config = self.config
        if not config.doc_ids:
            params = {"workspaceId": config.workspace_id, "folderId": config.folder_id}
            return list(self._items("/docs", {k: v for k, v in params.items() if v})), set()
        docs, refused = [], set()
        for doc_id in config.doc_ids:
            try:
                docs.append(self._call(self.client.get, f"/docs/{doc_id}"))
            except CodaAPIError as exc:
                if exc.status not in (*_DELETED, _FORBIDDEN):
                    raise
                if doc_id not in self.known:
                    # Never synced: most likely a typo or a doc the token was never given.
                    raise CodaSourceError(
                        f"Coda doc {doc_id} in doc_ids cannot be read (HTTP {exc.status})"
                    ) from exc
                if exc.status == _FORBIDDEN:
                    self._unreadable(doc_id)
                    refused.add(doc_id)
        return docs, refused

    # -- one doc -----------------------------------------------------------
    def _sync_doc(self, doc: dict, entry: dict) -> Iterator[dict]:
        """Yield one doc's changed pages and tables and their tombstones, updating ``entry``."""
        doc_id = doc["id"]
        # A renamed doc changes every document's header, so everything is rendered again.
        renamed = entry.get("name") != doc.get("name")
        entry["name"] = doc.get("name")

        listed_pages = list(self._items(f"/docs/{doc_id}/pages"))
        # Every Coda doc has at least one page, so an empty listing is not an answer:
        # its known pages are kept instead of being forgotten.
        keep_pages = not listed_pages
        if keep_pages and entry.get("pages"):
            logger.warning("Coda listed no pages for doc %s; its pages are left unchanged.", doc_id)
        pages = [p for p in listed_pages if _wanted_page(p, self.config)]
        tables = []
        if self.config.include_tables:
            listed = self._items(f"/docs/{doc_id}/tables", {"tableTypes": _TABLE_TYPES})
            details = (self._table(doc_id, table, entry) for table in listed)
            tables = [table for table in details if table is not None]

        yield from self._sync_kind(doc, "pages", pages, entry, renamed, keep_pages)
        yield from self._sync_kind(doc, "tables", tables, entry, renamed, False)

    def _sync_kind(
        self, doc: dict, kind: str, items: list[dict], entry: dict, renamed: bool, keep: bool
    ) -> Iterator[dict]:
        """Yield the changed pages or tables of one doc, then tombstones for the missing
        ones (unless ``keep``)."""
        known: dict[str, dict] = entry.setdefault(kind, {})
        listed: set[str] = set()
        for item in items:
            listed.add(item["id"])
            previous = known.get(item["id"])
            if previous and previous["updated"] == item.get("updatedAt") and not renamed:
                self.stats["unchanged"] += 1
                continue
            try:
                row = self._render_item(doc, kind, item)
            except CodaAPIError as exc:
                if exc.status == _FORBIDDEN:
                    # Access removed for this item only: keep what is known.
                    logger.warning("Coda: %s %s answered 403, left unchanged.", kind, item["id"])
                    continue
                if exc.status not in _DELETED:
                    raise
                # Deleted between the listing and this read: forget only this item.
                listed.discard(item["id"])
                continue
            known[item["id"]] = {"updated": item.get("updatedAt"), "hash": _digest(row)}
            if previous and previous["hash"] == known[item["id"]]["hash"]:
                self.stats["unchanged"] += 1
                continue
            self.stats["emitted"] += 1
            yield row

        if keep:
            return
        prefix = "page" if kind == "pages" else "table"
        for item_id in [i for i in known if i not in listed]:
            del known[item_id]
            self.stats["deleted"] += 1
            yield {"id": f"{prefix}:{doc['id']}:{item_id}", "_deleted": True}

    def _table(self, doc_id: str, table: dict, entry: dict) -> dict | None:
        """The table with its ``updatedAt`` and ``displayColumn``, which the listing lacks.

        ``None`` when the table was deleted after the listing, so it is forgotten.
        """
        try:
            return self._call(self.client.get, f"/docs/{doc_id}/tables/{table['id']}")
        except CodaAPIError as exc:
            if exc.status in _DELETED:
                return None
            if exc.status != _FORBIDDEN:
                raise
        logger.warning("Coda: table %s answered 403, left unchanged.", table["id"])
        known = (entry.get("tables") or {}).get(table["id"])
        # Access removed: a known table keeps its last version, an unknown one is skipped.
        return {**table, "updatedAt": known["updated"]} if known else None

    def _render_item(self, doc: dict, kind: str, item: dict) -> dict:
        if kind == "pages":
            self.stats["exported"] += 1
            markdown = self._call(self.client.export_markdown, doc["id"], item["id"])
            return render_page(doc, item, markdown)
        path = f"/docs/{doc['id']}/tables/{item['id']}"
        columns = list(self._items(f"{path}/columns", {"limit": _COLUMNS_PAGE}))
        rows = list(self._items(f"{path}/rows", {"valueFormat": "simpleWithArrays"}))
        return render_table(doc, item, columns, rows)

    # -- the run -----------------------------------------------------------
    def rows(self) -> Iterator[dict]:
        docs, refused = self._docs()
        listed = {doc["id"] for doc in docs} | refused
        yield from self._forget_unlisted(listed)
        # Start after the doc a cut-short run reached, so every one gets its turn.
        order = sorted(docs, key=lambda d: d["id"])
        resume = self.state.get("resume")
        if resume:
            order = [d for d in order if d["id"] > resume] + [d for d in order if d["id"] <= resume]
        for doc in order:
            # Work on a copy, so a doc that turns out to be unreadable keeps its old state.
            entry = copy.deepcopy(self.known.get(doc["id"])) or {}
            try:
                rows = list(self._sync_doc(doc, entry))
            except CodaAPIError as exc:
                if exc.status == _FORBIDDEN:
                    self._unreadable(doc["id"])
                elif exc.status in _DELETED:
                    # Deleted while this run was reading it.
                    self.stats["gone"] += 1
                    yield from _forget(doc["id"], self.known.pop(doc["id"], {}), self.stats)
                else:
                    raise
            else:
                self.known[doc["id"]] = entry
                yield from rows
            self.state["resume"] = doc["id"]
            self.progressed = True
        self.state.pop("resume", None)

    def _forget_unlisted(self, listed: set[str]) -> Iterator[dict]:
        """Docs that left the listing or the selection. Decided right after the doc
        list, so a run cut short by the budget still forgets them."""
        gone = [doc_id for doc_id in self.known if doc_id not in listed]
        discovered = not self.config.doc_ids
        if discovered and gone and not listed and not self.state.get("empty_listing"):
            # A /docs listing that is empty as a whole is confirmed by the next run;
            # a selected doc answering 404 or 410 is an explicit answer already.
            self.state["empty_listing"] = True
            logger.warning(
                "Coda listed no docs although %d were synced before; nothing is forgotten "
                "until the next run confirms it.",
                len(gone),
            )
            return
        self.state.pop("empty_listing", None)
        for doc_id in gone:
            yield from _forget(doc_id, self.known.pop(doc_id), self.stats)


def _iter_rows(
    client: Any,
    config: _CodaConfig,
    state: dict,
    stats: dict[str, int],
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict]:
    """Yield changed rows and tombstones. Pure of dlt, so tests drive it with a dict.

    A rejected token raises :class:`CodaAuthError`, which is never read as "gone".
    """
    for key in _STATS:
        stats.setdefault(key, 0)
    reset = getattr(client, "reset_sleep_budget", None)
    if reset is not None:
        reset()
    try:
        yield from _Walker(client, config, state, stats, check_active).rows()
    except _CutShortError as cut:
        # A spent budget is a planned pause, not a failure; failed_budget says why.
        if cut.reason != "budget":
            stats["failed"] = 1
        stats[f"failed_{cut.reason}"] = 1
        logger.warning("Coda sync stopped early (%s); it resumes on the next run.", cut.reason)
        return
    logger.info(
        "Coda: %d document(s) emitted, %d page export(s), %d deleted.",
        stats["emitted"],
        stats["exported"],
        stats["deleted"],
    )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    if slug != value:
        # Coda ids are case sensitive, so the raw value keeps them apart.
        slug += "_" + hashlib.sha256(value.encode()).hexdigest()[:8]
    return slug


def _default_resource_name(
    doc_ids: list[str] | None, workspace_id: str | None, folder_id: str | None
) -> str:
    if doc_ids and len(doc_ids) > 1:
        raise ValueError(
            "Pass a resource_name when syncing several docs: it scopes this sync's state "
            "and cleanup, and two sources sharing one would delete each other's documents."
        )
    if doc_ids:
        return "coda_" + _slug(doc_ids[0])
    scope = "_".join(_slug(part) for part in (workspace_id, folder_id) if part)
    return "coda_" + (scope or "my_docs")


def coda_source(
    doc_ids: list[str] | None = None,
    *,
    token: str | None = None,
    workspace_id: str | None = None,
    folder_id: str | None = None,
    include_tables: bool = True,
    include_hidden_pages: bool = False,
    include_sync_pages: bool = False,
    base_url: str | None = None,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    resource_name: str | None = None,
    check_active: Callable[[], None] | None = None,
    service: Any = None,
    client: Any = None,
):
    """Return a ``dlt`` resource that yields one document per Coda page and table.

    Hand the result to ``cognee.remember(...)`` with ``write_disposition="merge"``
    and ``primary_key="id"``.

    Args:
        doc_ids: Docs to sync. A listed doc that cannot be read on its first sync
            raises. Defaults to every doc the token can see, narrowed by
            ``workspace_id`` and ``folder_id``.
        token: Coda API token.
        workspace_id: Only docs of this workspace, when ``doc_ids`` is not given.
        folder_id: Only docs of this folder, when ``doc_ids`` is not given.
        include_tables: Ingest each table (not views) as one document.
        include_hidden_pages: Also ingest hidden pages.
        include_sync_pages: Also ingest sync pages, which mirror a page of another doc.
        base_url: API base URL, default ``https://coda.io/apis/v1``.
        max_requests: Request budget of one run, checked once a doc is finished. A
            bigger account finishes over several runs; a spent budget sets
            ``failed_budget`` but not ``failed``.
        resource_name: Staging table name, which scopes this sync's state and
            cleanup. Defaults to one derived from the doc, workspace or folder;
            required when ``doc_ids`` lists several docs.
        check_active: Optional host authorization checkpoint, called before each
            request and around each row.
        service: Pre-built client (see :class:`CodaClient`). Mainly an injection
            point for tests and hosts. ``client`` is accepted as an alias.

    Returns:
        A ``dlt`` resource configured with ``primary_key="id"``,
        ``write_disposition="merge"`` and a ``_deleted`` hard-delete column.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError('The Coda connector requires dlt: pip install "cognee[dlt]".') from exc

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError(
            "Coda sync requires a cognee build with table-scoped DLT document cleanup."
        )

    name = resource_name or _default_resource_name(doc_ids, workspace_id, folder_id)
    service = service or client
    if service is None:
        if not token:
            raise ValueError("A Coda API token is required: pass token=, or a pre-built service.")
        service = CodaClient(token=token, base_url=base_url or DEFAULT_BASE_URL)

    config = _CodaConfig(
        doc_ids=tuple(doc_ids or ()),
        workspace_id=workspace_id,
        folder_id=folder_id,
        include_tables=include_tables,
        include_hidden_pages=include_hidden_pages,
        include_sync_pages=include_sync_pages,
        max_requests=int(max_requests),
    )
    stats: dict[str, int] = {}

    @dlt.resource(
        name=name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def coda_documents():
        stats.clear()
        state = dlt.current.resource_state()
        rows = _iter_rows(service, config, state, stats, check_active)
        yield from dlt_utils.guarded_rows(rows, check_active)

    resource = coda_documents()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "coda")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, name)
    resource.cognee_sync_stats = stats
    return resource
