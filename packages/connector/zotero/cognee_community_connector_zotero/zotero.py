"""Atomic Zotero snapshots rendered as cognee documents."""

import json
import math
import os
import time
from contextlib import nullcontext
from html.parser import HTMLParser

import dlt
import httpx
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from dlt.common.exceptions import PipelineStateNotAvailable

logger = get_logger("zotero_connector")
_BASE_URL = "https://api.zotero.org"
_MAX_RETRIES = 5


class ZoteroLibraryUnchanged(Exception):  # noqa: N818 - public no-op signal
    """The version probe found no changes; no replace load should run."""

    def __init__(self, version):
        self.version = version
        super().__init__(f"Library unchanged since version {version}; staging untouched")


class ZoteroSyncError(Exception):
    """A complete snapshot could not be established; staging stays untouched."""


def zotero_source(
    library_id: str,
    zotero_api_key: str | None = None,
    library_type: str = "group",
    include_notes: bool = True,
    include_attachment_text: bool = True,
    page_size: int = 100,
    pacing_seconds: float = 0.3,
    client: httpx.Client | None = None,
    clock=None,
):
    """Build a full-snapshot source; injected clients remain caller-owned.

    Reuse the same dlt pipeline for persisted watermarks. A 304 probe raises
    ZoteroLibraryUnchanged before any rows are yielded or loaded. The probe
    runs at construction when state is available, otherwise during extraction.
    clock supplies monotonic() and sleep(seconds), primarily for offline tests.
    """
    if library_type not in ("user", "group"):
        raise ValueError("library_type must be user or group")
    if not isinstance(library_id, str) or not library_id.isdecimal():
        raise ValueError("library_id must be a numeric ID string")
    if type(page_size) is not int or not 1 <= page_size <= 100:
        raise ValueError("page_size must be an integer between 1 and 100")
    if not math.isfinite(pacing_seconds) or pacing_seconds < 0:
        raise ValueError("pacing_seconds must be finite and nonnegative")
    key = zotero_api_key or os.environ.get("ZOTERO_API_KEY")
    if library_type == "user" and not key:
        raise ValueError("user libraries require an API key: set ZOTERO_API_KEY")
    prefix = f"/{library_type}s/{library_id}"
    headers = {"Zotero-API-Version": "3"}
    if key:
        headers["Zotero-API-Key"] = key
    timer = clock or time
    # Persist pacing across the probe and snapshot, even with separate owned clients.
    requester = _Requester(headers, prefix, pacing_seconds, timer)
    scope = f"{library_type}/{library_id}/notes={include_notes}/text={include_attachment_text}"

    probed_watermark = None

    def context():
        return nullcontext(client) if client is not None else httpx.Client(timeout=60)

    def probe(http, watermark):
        response = requester(
            http,
            prefix + "/items",
            params={"limit": 1, "format": "json"},
            headers={"If-Modified-Since-Version": str(watermark)},
        )
        if response.status_code == 304:
            raise ZoteroLibraryUnchanged(watermark)

    @dlt.resource(name="documents", primary_key="id", write_disposition="replace")
    def documents():
        with context() as http:
            # Cognee may activate its persistent pipeline only after source creation.
            watermark = dlt.current.source_state().get("watermarks", {}).get(scope)
            if watermark is not None and watermark != probed_watermark:
                probe(http, watermark)
            try:
                items, version = _items(requester, http, page_size)
                response = requester(
                    http, prefix + "/collections", params={"format": "json", "limit": 100}
                )
                _version(response, version)
                collections = response.json()
                if not isinstance(collections, list):
                    raise ZoteroSyncError("Invalid collections snapshot")
                if response.links.get("next") or (
                    "Total-Results" in response.headers
                    and int(response.headers["Total-Results"]) != len(collections)
                ):
                    raise ZoteroSyncError("Incomplete collections snapshot")
                names = {c["key"]: c["data"]["name"] for c in collections}
                titles = {i["key"]: i["data"].get("title", "") for i in items}
                rows = []
                for item in items:
                    data = item["data"]
                    if data["itemType"] == "note" and not include_notes:
                        continue
                    if data.get("parentItem") and data["itemType"] not in ("note", "attachment"):
                        continue
                    row = _row(item, library_type, library_id, names, titles)
                    if (
                        include_attachment_text
                        and data["itemType"] == "attachment"
                        and data.get("linkMode") in ("imported_file", "imported_url")
                    ):
                        fulltext = requester(
                            http, prefix + f"/items/{item['key']}/fulltext", absent_ok=True
                        )
                        text = (
                            fulltext.json().get("content", "")
                            if fulltext.status_code == 200
                            else ""
                        )
                        if text:
                            row["content"] += "\n" + text
                        else:
                            logger.info("Zotero: fulltext absent for %s", item["key"])
                    rows.append(row)
            except ZoteroSyncError:
                raise
            except (ValueError, KeyError, TypeError) as exc:
                raise ZoteroSyncError("Invalid Zotero snapshot; staging untouched") from exc
            yield rows
            dlt.current.source_state()["watermarks"] = {scope: version}

    @dlt.source(name="zotero")
    def _zotero():
        nonlocal probed_watermark
        try:
            watermark = dlt.current.source_state().get("watermarks", {}).get(scope)
        except PipelineStateNotAvailable:
            watermark = None
        if watermark is not None:
            with context() as http:
                probe(http, watermark)
            probed_watermark = watermark
        return documents

    source = _zotero()
    setattr(source, DOCUMENT_SOURCE_ATTR, "zotero")
    return source


class _Requester:
    def __init__(self, headers, prefix, interval, clock):
        self.headers, self.prefix = headers, prefix
        self.interval, self.clock = interval, clock
        self.ready_at = 0.0

    def __call__(self, client, path, *, params=None, headers=None, absent_ok=False):
        url = httpx.URL(_BASE_URL + path) if path.startswith("/") else httpx.URL(path)
        if (
            url.scheme != "https"
            or url.host != "api.zotero.org"
            or not url.path.startswith(self.prefix + "/")
        ):
            raise ZoteroSyncError("Pagination link leaves the requested library")

        def get():
            delay = self.ready_at - self.clock.monotonic()
            if delay > 0:
                self.clock.sleep(delay)
            self.ready_at = self.clock.monotonic() + self.interval
            response = client.get(url, params=params, headers={**self.headers, **(headers or {})})
            backoff = _seconds(response.headers.get("Backoff"), 0)
            self.ready_at = max(self.ready_at, self.clock.monotonic() + backoff)
            if response.status_code == 403:
                raise ZoteroSyncError(f"API key invalid or no access to library {self.prefix}")
            if response.status_code == 404 and not absent_ok:
                raise ZoteroSyncError("library not found or not readable")
            if response.status_code == 304 or (absent_ok and response.status_code == 404):
                return response
            response.raise_for_status()
            return response

        return _request(get, self.clock)


def _seconds(value, fallback):
    try:
        number = float(value)
        return max(0, number) if math.isfinite(number) else fallback
    except (TypeError, ValueError):
        return fallback


def _retry_after(headers, attempt):
    return _seconds((headers or {}).get("Retry-After"), float(2**attempt))


def _is_transient(exc):
    return isinstance(exc, httpx.TransportError) or (
        isinstance(exc, httpx.HTTPStatusError)
        and (exc.response.status_code == 429 or 500 <= exc.response.status_code < 600)
    )


def _request(method, clock):
    for attempt in range(_MAX_RETRIES):
        try:
            return method()
        except (httpx.HTTPError, ZoteroSyncError) as exc:
            if not _is_transient(exc) or attempt == _MAX_RETRIES - 1:
                if isinstance(exc, ZoteroSyncError):
                    raise
                raise ZoteroSyncError("Zotero request failed; staging untouched") from exc
            response = getattr(exc, "response", None)
            headers = response.headers if response is not None else None
            clock.sleep(_retry_after(headers, attempt))


def _version(response, expected=None):
    version = int(response.headers["Last-Modified-Version"])
    if version < 0 or (expected is not None and version != expected):
        raise ZoteroSyncError("Library changed during snapshot; staging untouched")
    return version


def _items(request, client, page_size):
    url = request.prefix + "/items"
    params = {"limit": page_size, "start": 0, "format": "json"}
    rows, seen = [], set()
    version = total = None
    while url:
        if url in seen:
            raise ZoteroSyncError("Repeated pagination link")
        seen.add(url)
        response = request(client, url, params=params)
        version = _version(response, version)
        count = int(response.headers["Total-Results"])
        if count < 0 or (total is not None and count != total):
            raise ZoteroSyncError("Total-Results changed during snapshot")
        total = count
        page = response.json()
        if not isinstance(page, list):
            raise ZoteroSyncError("Invalid items snapshot")
        rows.extend(page)
        url = response.links.get("next", {}).get("url")
        params = None
        if len(rows) > total or (url and not page):
            raise ZoteroSyncError("Incomplete items snapshot")
    if len(rows) != total or len({r["key"] for r in rows}) != total:
        raise ZoteroSyncError("Incomplete or duplicate items snapshot")
    return rows, version


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ("p", "div", "br", "li"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("p", "div", "li"):
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def _row(item, library_type, library_id, names, titles):
    data = item["data"]
    creators = data.get("creators", [])
    authors = "; ".join(
        ", ".join(filter(None, [c.get("lastName"), c.get("firstName")])) or c.get("name", "")
        for c in creators
    )
    tags = ", ".join(t["tag"] for t in data.get("tags", []))
    collections = ", ".join(names.get(key, key) for key in data.get("collections", []))
    title = data.get("title", "")
    parent = titles.get(data.get("parentItem"), "")
    parts = [
        title,
        authors,
        data.get("date", ""),
        data.get("publicationTitle", ""),
        data.get("DOI", ""),
        data.get("url", ""),
        tags,
        collections,
        data.get("abstractNote", ""),
    ]
    if data["itemType"] == "note":
        parser = _PlainText()
        parser.feed(data.get("note", ""))
        parts = [parent, "".join(parser.parts).strip()]
    elif data["itemType"] == "attachment":
        parts = [
            data.get("filename", ""),
            data.get("contentType", ""),
            data.get("linkMode", ""),
            parent,
        ]
    return {
        "id": f"zotero://{library_type}/{library_id}/items/{item['key']}",
        "title": title,
        "content": "\n".join(p for p in parts if p),
        "item_key": item["key"],
        "item_type": data["itemType"],
        "version": item["version"],
        "doi": data.get("DOI", ""),
        "url": data.get("url", ""),
        "pub_date": data.get("date", ""),
        "journal": data.get("publicationTitle", ""),
        "creators": json.dumps(creators, sort_keys=True),
        "tags": tags,
        "collections": collections,
    }
