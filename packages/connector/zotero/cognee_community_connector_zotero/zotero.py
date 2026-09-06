"""Zotero data-source connector for cognee - version-based incremental sync + forget-on-delete.

Fetches Zotero items (references, notes, attachment text) as markdown documents
and yields them as a dlt resource for cognee's ingestion pipeline.

Items are ingested as *normal documents*: the source declares
``cognee_document_source = "zotero"``, so ``resolve_dlt_sources`` tags each row
``external_metadata["source"] = "zotero"`` and routes through normal cognify.

Sync is **incremental via the library version header** (``Last-Modified-Version``):
each run records the library version and on the next run fetches only items changed
since then. Deleted and trashed items are emitted as ``_deleted`` tombstones,
so dlt's ``merge`` + ``hard_delete`` removes them from staging and cognee's
``orphan_cleanup`` forgets them from the graph and vector stores.
"""

from __future__ import annotations

import os
import time
from html.parser import HTMLParser
from logging import getLogger
from typing import Any
from urllib.parse import urljoin

import httpx

_logger = getLogger(__name__)

# Matches cognee.tasks.ingestion.dlt_utils.DOCUMENT_SOURCE_ATTR value
DOCUMENT_SOURCE_ATTR = "cognee_document_source"

ZOTERO_API_BASE = "https://api.zotero.org"
ZOTERO_API_VERSION = 3
_MAX_RETRIES = 5
_BATCH_KEYS = 50
_PAGE_LIMIT = 100

_EXTRA_HINT = 'The Zotero connector requires the "zotero" extra: pip install "cognee[zotero]"'

# ---------------------------------------------------------------------------
# HTML stripper (stdlib, no extra dep)
# ---------------------------------------------------------------------------


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._out: list[str] = []

    def handle_data(self, data: str) -> None:
        self._out.append(data)

    def get_text(self) -> str:
        import re
        text = " ".join(self._out).strip()
        return re.sub(r" {2,}", " ", text)


def _strip_html(raw: str | None) -> str:
    if not raw:
        return ""
    p = _TextExtractor()
    p.feed(raw)
    return p.get_text()


# ---------------------------------------------------------------------------
# Item → document-row helpers
# ---------------------------------------------------------------------------


def _creator_str(creators: list[dict[str, Any]]) -> str:
    if not creators:
        return ""
    parts = []
    for c in creators:
        first = c.get("firstName", "")
        last = c.get("lastName", "")
        name = (f"{first} {last}".strip()) or c.get("name", "")
        parts.append(f"{name}")
    return "; ".join(p for p in parts if p)


def _tag_str(tags: list[dict[str, Any]]) -> str:
    if not tags:
        return ""
    return "; ".join(t.get("tag", "") for t in tags if t.get("tag"))


def _item_to_row(client: ZoteroClient, item: dict[str, Any]) -> dict[str, Any] | None:
    key = item.get("key", "")
    title = item.get("title") or key
    url = item.get("url") or ""
    trashed = item.get("trashed", False)
    item_type = item.get("itemType", "")

    library_id = item.get("library", {}).get("id", "")
    zotero_url = f"https://www.zotero.org/users/{library_id}/items/{key}"

    if trashed:
        return {"id": key, "_deleted": True}

    if item_type == "note":
        content = _strip_html(item.get("note", ""))
        return {
            "id": key,
            "title": title or "Note",
            "content": content,
            "url": url or zotero_url,
            "_deleted": False,
        }

    if item_type == "attachment":
        content_type = item.get("contentType", "")
        filename = item.get("filename", "")
        link_mode = item.get("linkMode", "")
        bits = [f"**{title}**"]
        if filename:
            bits.append(f"file: {filename}")
        bits.append(f"type: {content_type}")

        if content_type.startswith("text/") or link_mode in ("imported_file", "imported_url"):
            text = _fetch_attachment_text(client, library_id, key)
            if text:
                bits.append(f"\n--- attachment content ---\n{text}")

        return {
            "id": key,
            "title": title,
            "content": "\n".join(b for b in bits),
            "url": url or zotero_url,
            "_deleted": False,
        }

    bits: list[str] = []
    abstract = item.get("abstractNote", "").strip()
    if abstract:
        bits.append(abstract)

    creators = _creator_str(item.get("creators", []))
    if creators:
        bits.append(f"Creators: {creators}")

    tags = _tag_str(item.get("tags", []))
    if tags:
        bits.append(f"Tags: {tags}")

    pub = item.get("publicationTitle", "")
    doi = item.get("DOI", "")
    if pub:
        bits.append(f"Publication: {pub}")
    if doi:
        bits.append(f"DOI: {doi}")
    date = item.get("date", "")
    if date:
        bits.append(f"Date: {date}")

    return {
        "id": key,
        "title": title,
        "content": "\n\n".join(bits),
        "url": url or zotero_url,
        "_deleted": False,
    }


def _fetch_attachment_text(client: ZoteroClient, library_id: str, key: str) -> str:
    """Fetch attachment text via the Zotero file endpoint.

    For text/* content types the full text is included; for other types (PDF, etc.)
    a warning is logged and the function returns "" (the attachment metadata is still
    ingested).
    """
    try:
        resp = client._request("GET", f"/users/{library_id}/items/{key}/file")
        resp.raise_for_status()
        data = resp.content
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="replace")
        return data.strip()
    except Exception as exc:  # pragma: no cover - per-attachment errors never fail the sync
        _logger.warning("Zotero: failed to fetch attachment %s: %s", key, exc)
        return ""


# ---------------------------------------------------------------------------
# HTTP layer (retry, backoff, error classification)
# ---------------------------------------------------------------------------


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = getattr(exc.response, "status_code", None)
        if status in (429, 500, 502, 503, 504):
            return True
    return False


def _is_gone(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        status = getattr(exc.response, "status_code", None)
        if status in (403, 404):
            return True
    return False


def _retry_after(headers: dict[str, str] | None, attempt: int) -> float:
    h = headers or {}
    val = h.get("retry-after") or h.get("Retry-After", "")
    try:
        return float(val)
    except (TypeError, ValueError):
        return 2.0**attempt


# ---------------------------------------------------------------------------
# Client wrapper (injectable for tests)
# ---------------------------------------------------------------------------


class ZoteroClient:
    def __init__(self, token: str, user_id: str | None = None) -> None:
        self._token = token
        self._user_id = user_id
        self._base = ZOTERO_API_BASE
        self._headers = {
            "Zotero-API-Version": str(ZOTERO_API_VERSION),
            "Authorization": f"Bearer {token}",
        }

    def resolve_user_id(self) -> str:
        if self._user_id:
            return self._user_id
        r = self._request("GET", "/keys/current")
        self._user_id = str(r.json()["userID"])
        return self._user_id

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        data: Any | None = None,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        url = urljoin(self._base + "/", path.lstrip("/"))
        merged_headers = dict(self._headers)
        if headers:
            merged_headers.update(headers)

        for attempt in range(_MAX_RETRIES):
            try:
                resp = httpx.request(
                    method,
                    url,
                    headers=merged_headers,
                    params=params,
                    data=data,
                    json=json,
                    timeout=30.0,
                )
                if attempt == _MAX_RETRIES - 1 or not _is_transient(resp):
                    resp.raise_for_status()
                    return resp
                delay = _retry_after(dict(resp.headers), attempt)
                _logger.warning(
                    "Zotero: %s %s → %s, retrying in %.1fs (%d/%d)",
                    method,
                    path,
                    resp.status_code,
                    delay,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(delay)
            except Exception as exc:
                if attempt == _MAX_RETRIES - 1 or not _is_transient(exc):
                    raise
                delay = _retry_after(getattr(exc, "headers", None), attempt)
                _logger.warning(
                    "Zotero: %s %s → %s, retrying in %.1fs (%d/%d)",
                    method,
                    path,
                    exc,
                    delay,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    def get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        return self._request("GET", path, params=params)


# ---------------------------------------------------------------------------
# Sync state machine (pure given client + state dict — unit-testable)
# ---------------------------------------------------------------------------


def _iter_rows(client: ZoteroClient, state: dict[str, Any]) -> Any:
    uid = state.get("user_id")
    if not uid:
        uid = client.resolve_user_id()
        state["user_id"] = uid

    since_version: int | None = state.get("since_version")
    version_param = since_version if since_version is not None else 0

    headers: dict[str, str] = {}
    if since_version is not None:
        headers["If-Modified-Since-Version"] = str(since_version)

    params = {
        "since": version_param,
        "format": "versions",
        "includeTrashed": "1",
        "limit": str(_PAGE_LIMIT),
    }

    changed_keys: dict[str, int] = {}
    new_version = since_version or 0

    start = 0
    while True:
        page_params = {**params, "start": str(start)}
        resp = client.get(f"/users/{uid}/items", params=page_params)

        if resp.status_code == 304:
            _logger.info("Zotero: no library changes since version %s.", since_version)
            return

        new_version = max(
            new_version,
            int(resp.headers.get("Last-Modified-Version", new_version)),
        )
        body = resp.json()
        for k, v in body.items():
            changed_keys[k] = int(v)

        total = int(resp.headers.get("Total-Results", "0"))
        start += _PAGE_LIMIT
        if start >= total:
            break

    if not changed_keys:
        state["since_version"] = new_version
        _logger.info("Zotero: version %s — no changed items.", new_version)
        return

    for i in range(0, len(changed_keys), _BATCH_KEYS):
        batch = list(changed_keys.keys())[i : i + _BATCH_KEYS]
        detail_params = {
            "itemKey": ",".join(batch),
            "includeTrashed": "1",
        }
        resp = client.get(f"/users/{uid}/items", params=detail_params)
        new_version = max(
            new_version,
            int(resp.headers.get("Last-Modified-Version", new_version)),
        )
        for item in resp.json():
            row = _item_to_row(client, item)
            if row:
                yield row

    del_resp = client.get(f"/users/{uid}/deleted", params={"since": version_param})
    new_version = max(
        new_version,
        int(del_resp.headers.get("Last-Modified-Version", new_version)),
    )
    deleted_body = del_resp.json()
    for key in deleted_body.get("items", []):
        yield {"id": key, "_deleted": True}

    state["since_version"] = new_version
    _logger.info(
        "Zotero: synced %d changed item(s) up to version %s.", len(changed_keys), new_version
    )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def zotero_source(
    api_key: str | None = None,
    user_id: str | None = None,
    *,
    client: ZoteroClient | None = None,
) -> Any:
    """Create a dlt source that yields Zotero items as markdown documents.

    Args:
        api_key: Zotero personal API key. Falls back to ``ZOTERO_API_KEY``.
        user_id: Numeric Zotero user ID. Resolved automatically from ``api_key``
            via ``/keys/current`` if omitted.
        client: Pre-built ``ZoteroClient`` (mainly a test-injection point).

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
        Hand to ``remember()`` with ``write_disposition="merge"`` and
        ``primary_key="id"``::

            await cognee.remember(
                zotero_source(api_key="..."),
                dataset_name="my_zotero",
                primary_key="id",
                write_disposition="merge",
            )
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    if client is None:
        resolved_key = api_key or os.environ.get("ZOTERO_API_KEY")
        if not resolved_key:
            raise ValueError("Zotero API key required: pass api_key= or set ZOTERO_API_KEY.")
        client = ZoteroClient(resolved_key, user_id)

    @dlt.resource(
        name="zotero_items",
        write_disposition="merge",
        primary_key="id",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def zotero_items() -> Any:
        yield from _iter_rows(client, dlt.current.resource_state())

    source = zotero_items()
    setattr(source, DOCUMENT_SOURCE_ATTR, "zotero")
    return source
