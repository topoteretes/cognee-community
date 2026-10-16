"""Coda connector for cognee: a ``dlt`` source that turns Coda docs into memory.

Sync the pages and tables of Coda docs into cognee, incrementally and with
forget-on-delete. The resource is handed directly to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_coda import coda_source

    await cognee.remember(
        coda_source(doc_ids=["<doc id>"]),  # CODA_API_TOKEN from env
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
  last run. Only changed pages are exported again and only changed tables have
  their rows read again.
* **Forget-on-delete.** A page or table missing from its doc's listing, and a doc
  that is deleted, no longer shared or no longer selected, are emitted as
  hard-delete tombstones. Any other failure raises, and dlt rolls the state of a
  failed run back.

Privacy
-------
This reads the content of every selected doc. Nothing is fetched until you
construct a source and call ``remember``, and anyone with read access to the
target dataset can read what was ingested.
"""

import copy
import hashlib
import json
import logging
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://coda.io/apis/v1"
_TIMEOUT = 30.0
_RETRIES = 5
_MAX_RETRY_SLEEP = 60.0
_EXPORT_TIMEOUT = 120.0
_COLUMNS_PAGE = 100
# Views show rows of a base table that is synced already.
_TABLE_TYPES = "table,database"
_GONE = (403, 404, 410)


# ---------------------------------------------------------------------------
# Errors: messages carry the HTTP status only, never the token, links or bodies.
# ---------------------------------------------------------------------------
class CodaSourceError(RuntimeError):
    """Base class of the errors this source raises."""


class CodaAuthError(CodaSourceError):
    """Coda rejected the API token (HTTP 401)."""


class CodaAPIError(CodaSourceError):
    """Coda answered with an error, or could not be reached."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
def _retry_delay(response: Any, attempt: int) -> float:
    try:
        delay = float(response.headers.get("Retry-After"))
    except (TypeError, ValueError):
        delay = float(2**attempt)
    return min(max(delay, 0.0), _MAX_RETRY_SLEEP)


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

    def __repr__(self) -> str:
        return "CodaClient(token=<redacted>)"

    def _send(self, method: str, url: str, headers: dict[str, str], **kwargs: Any) -> Any:
        import httpx

        for attempt in range(_RETRIES):
            last = attempt == _RETRIES - 1
            try:
                response = self._http.request(method, url, headers=headers, **kwargs)
            except httpx.TransportError as exc:
                if last:
                    raise CodaAPIError(f"Coda request failed: {type(exc).__name__}") from None
                self._sleep(float(2**attempt))
                continue
            status = response.status_code
            if (status == 429 or status >= 500) and not last:
                self._sleep(_retry_delay(response, attempt))
                continue
            if status == 401:
                raise CodaAuthError("Coda rejected the API token (HTTP 401)")
            if status not in (200, 202):
                raise CodaAPIError(f"Coda request failed: HTTP {status}", status=status)
            return response
        raise CodaAPIError("Coda request failed")  # pragma: no cover

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


def _items(client: Any, path: str, params: dict[str, Any] | None = None) -> Iterator[dict]:
    """Yield every item of a paginated Coda list."""
    token = None
    while True:
        page = client.get(path, {**(params or {}), **({"pageToken": token} if token else {})})
        yield from page.get("items") or []
        next_token = page.get("nextPageToken")
        if not next_token:
            return
        if next_token == token:
            raise CodaAPIError("Coda pagination did not advance")
        token = next_token


def _docs(client: Any, config: _CodaConfig) -> list[dict]:
    if not config.doc_ids:
        params = {"workspaceId": config.workspace_id, "folderId": config.folder_id}
        return list(_items(client, "/docs", {k: v for k, v in params.items() if v}))
    docs = []
    for doc_id in config.doc_ids:
        try:
            docs.append(client.get(f"/docs/{doc_id}"))
        except CodaAPIError as exc:
            # Deleted or no longer shared: it is forgotten below.
            if exc.status not in _GONE:
                raise
    return docs


def _wanted_page(page: dict, config: _CodaConfig) -> bool:
    kind = page.get("contentType") or "canvas"
    if kind == "embed" or (kind == "syncPage" and not config.include_sync_pages):
        return False
    return config.include_hidden_pages or not page.get("isEffectivelyHidden")


def _sync_doc(
    client: Any, config: _CodaConfig, doc: dict, entry: dict, stats: dict[str, int]
) -> Iterator[dict]:
    """Yield one doc's changed pages and tables and their tombstones, updating ``entry``."""
    doc_id = doc["id"]
    # A renamed doc changes every document's header, so everything is rendered again.
    renamed = entry.get("name") != doc.get("name")
    entry["name"] = doc.get("name")

    pages = [p for p in _items(client, f"/docs/{doc_id}/pages") if _wanted_page(p, config)]
    tables = []
    if config.include_tables:
        tables = list(_items(client, f"/docs/{doc_id}/tables", {"tableTypes": _TABLE_TYPES}))

    for kind, items in (("pages", pages), ("tables", tables)):
        known: dict[str, dict] = entry.setdefault(kind, {})
        listed: set[str] = set()
        for item in items:
            previous = known.get(item["id"])
            if previous and previous["updated"] == item.get("updatedAt") and not renamed:
                listed.add(item["id"])
                stats["unchanged"] += 1
                continue
            try:
                row = _render_item(client, doc, kind, item, stats)
            except CodaAPIError as exc:
                # Deleted between the listing and this read: forget only this item.
                if exc.status not in _GONE:
                    raise
                continue
            listed.add(item["id"])
            known[item["id"]] = {"updated": item.get("updatedAt"), "hash": _digest(row)}
            if previous and previous["hash"] == known[item["id"]]["hash"]:
                stats["unchanged"] += 1
                continue
            stats["emitted"] += 1
            yield row

        prefix = "page" if kind == "pages" else "table"
        for item_id in [i for i in known if i not in listed]:
            del known[item_id]
            stats["deleted"] += 1
            yield {"id": f"{prefix}:{doc_id}:{item_id}", "_deleted": True}


def _render_item(client: Any, doc: dict, kind: str, item: dict, stats: dict[str, int]) -> dict:
    if kind == "pages":
        stats["exported"] += 1
        return render_page(doc, item, client.export_markdown(doc["id"], item["id"]))
    path = f"/docs/{doc['id']}/tables/{item['id']}"
    columns = list(_items(client, f"{path}/columns", {"limit": _COLUMNS_PAGE}))
    rows = list(_items(client, f"{path}/rows", {"valueFormat": "simpleWithArrays"}))
    return render_table(doc, item, columns, rows)


def _forget(doc_id: str, entry: dict, stats: dict[str, int]) -> Iterator[dict]:
    for kind, prefix in (("pages", "page"), ("tables", "table")):
        for item_id in entry.get(kind) or {}:
            stats["deleted"] += 1
            yield {"id": f"{prefix}:{doc_id}:{item_id}", "_deleted": True}


def _iter_rows(client: Any, config: _CodaConfig, state: dict, stats: dict[str, int]):
    """Yield changed rows and tombstones. Pure of dlt, so tests drive it with a dict.

    A rejected token raises :class:`CodaAuthError`, which is never read as "gone".
    """
    known: dict[str, dict] = state.setdefault("docs", {})
    seen: set[str] = set()
    for doc in _docs(client, config):
        # Work on a copy, so a doc that turns out to be gone keeps its old state.
        entry = copy.deepcopy(known.get(doc["id"])) or {}
        try:
            rows = list(_sync_doc(client, config, doc, entry, stats))
        except CodaAPIError as exc:
            # Deleted or unshared while this run was reading it.
            if exc.status not in _GONE:
                raise
            stats["gone"] += 1
            continue
        known[doc["id"]] = entry
        seen.add(doc["id"])
        yield from rows

    # Deleted, unshared or deselected docs: forget every page and table they held.
    for doc_id in [d for d in known if d not in seen]:
        yield from _forget(doc_id, known.pop(doc_id), stats)

    logger.info(
        "Coda: %d document(s) emitted, %d page export(s), %d deleted.",
        stats["emitted"],
        stats["exported"],
        stats["deleted"],
    )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
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
    resource_name: str = "coda_documents",
    check_active: Callable[[], None] | None = None,
    client: Any = None,
):
    """Return a ``dlt`` resource that yields one document per Coda page and table.

    Hand the result to ``cognee.remember(...)`` with ``write_disposition="merge"``
    and ``primary_key="id"``.

    Args:
        doc_ids: Docs to sync. Defaults to every doc the token can see, narrowed by
            ``workspace_id`` and ``folder_id``.
        token: Coda API token (``CODA_API_TOKEN``).
        workspace_id: Only docs of this workspace, when ``doc_ids`` is not given.
        folder_id: Only docs of this folder, when ``doc_ids`` is not given.
        include_tables: Ingest each table (not views) as one document.
        include_hidden_pages: Also ingest hidden pages.
        include_sync_pages: Also ingest sync pages, which mirror a page of another doc.
        base_url: API base URL (``CODA_API_BASE_URL``), default ``https://coda.io/apis/v1``.
        resource_name: Staging table name. Use a different one per sync scope
            that shares a dataset.
        check_active: Optional host authorization checkpoint, called around each row.
        client: Pre-built client (see :class:`CodaClient`). Mainly an injection
            point for tests and hosts.

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

    if client is None:
        resolved_token = token or os.getenv("CODA_API_TOKEN")
        if not resolved_token:
            raise ValueError("A Coda API token is required (pass token= or set CODA_API_TOKEN).")
        client = CodaClient(
            resolved_token, base_url or os.getenv("CODA_API_BASE_URL") or DEFAULT_BASE_URL
        )

    config = _CodaConfig(
        doc_ids=tuple(doc_ids or ()),
        workspace_id=workspace_id,
        folder_id=folder_id,
        include_tables=include_tables,
        include_hidden_pages=include_hidden_pages,
        include_sync_pages=include_sync_pages,
    )
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def coda_documents():
        stats.clear()
        stats.update(emitted=0, unchanged=0, exported=0, deleted=0, gone=0)
        rows = _iter_rows(client, config, dlt.current.resource_state(), stats)
        yield from dlt_utils.guarded_rows(rows, check_active)

    resource = coda_documents()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "coda")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    resource.cognee_sync_stats = stats
    return resource
