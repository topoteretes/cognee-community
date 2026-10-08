"""Complete selected snapshots with incremental HTML fetching and conservative deletion."""

import copy
import hashlib
import json
import logging
import math
import time
from collections.abc import Callable
from urllib.parse import quote, urlsplit

import dlt
import httpx
from bs4 import BeautifulSoup
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR

_BASE = "https://graph.microsoft.com/v1.0/me/onenote"
_RENDERER_VERSION = 1
logger = logging.getLogger(__name__)
Token = str | Callable[[], str]


class OneNoteError(RuntimeError):
    """Graph failure with safe diagnostics (never response bodies or bearer tokens)."""

    def __init__(self, message: str, *, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


def _identity(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def _path(value: str) -> str:
    return quote(value, safe="")


class _Graph:
    def __init__(self, token: Token, client: httpx.Client | None):
        self.token = token
        self.client = client or httpx.Client()
        self.owned = client is None

    def close(self):
        if self.owned:
            self.client.close()

    def request(self, url: str, *, html: bool = False):
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "graph.microsoft.com"
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or not parsed.path.startswith("/v1.0/me/onenote/")
        ):
            raise OneNoteError("Unsafe Graph URL rejected")
        for attempt in range(5):
            token = self.token() if callable(self.token) else self.token
            if not isinstance(token, str) or not token.strip():
                raise OneNoteError("A delegated access token is required")
            try:
                response = self.client.get(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=30,
                    follow_redirects=False,
                )
            except httpx.TransportError:
                if attempt == 4:
                    raise OneNoteError("Graph transport failed after five attempts") from None
                time.sleep(2**attempt)
                continue
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 4:
                try:
                    delay = float(response.headers.get("Retry-After", 2**attempt))
                    if not math.isfinite(delay) or delay < 0:
                        delay = 2**attempt
                except ValueError:
                    delay = 2**attempt
                time.sleep(delay)
                continue
            if response.status_code != 200:
                code = ""
                try:
                    code = str(response.json().get("error", {}).get("code", ""))
                except (ValueError, AttributeError):
                    pass
                message = (
                    "Graph rejected the token; reauthenticate the selected account"
                    if response.status_code == 401
                    else "Graph request failed; snapshot was not authorized for replacement"
                )
                raise OneNoteError(message, status=response.status_code, code=code)
            if html:
                if "text/html" not in response.headers.get("content-type", "").lower():
                    raise OneNoteError("Graph page content is not HTML")
                return response.text
            try:
                result = response.json()
            except ValueError:
                raise OneNoteError("Malformed Graph JSON") from None
            if not isinstance(result, dict):
                raise OneNoteError("Expected a Graph object")
            return result
        raise OneNoteError("Graph retry budget exhausted")

    def collection(self, path: str):
        base = f"{_BASE}/{path}"
        url = f"{base}?$top=100&$skip=0"
        seen_urls, seen_ids = set(), set()
        offset = 0
        while True:
            if url in seen_urls:
                raise OneNoteError("Graph pagination cycle")
            seen_urls.add(url)
            response = self.request(url)
            rows = response.get("value")
            if not isinstance(rows, list):
                raise OneNoteError("Graph collection has no value array")
            for row in rows:
                _require(row, "id")
                if row["id"] in seen_ids:
                    raise OneNoteError("Graph collection changed or repeated during pagination")
                seen_ids.add(row["id"])
                yield row
            offset += len(rows)
            next_link = response.get("@odata.nextLink")
            if next_link is not None:
                if not isinstance(next_link, str) or not rows:
                    raise OneNoteError("Invalid Graph next link")
                url = next_link
            elif len(rows) == 100:
                url = f"{base}?$top=100&$skip={offset}"
            else:
                return


def _require(row, *fields):
    if not isinstance(row, dict) or any(
        not isinstance(row.get(field), str) or not row[field].strip() for field in fields
    ):
        raise OneNoteError("Graph metadata is missing required fields")


def list_notebooks(token: Token, *, http_client: httpx.Client | None = None) -> list[dict]:
    """List notebook metadata for explicit selection; does not download page content."""
    graph = _Graph(token, http_client)
    try:
        return list(graph.collection("notebooks"))
    finally:
        graph.close()


def _body(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    if soup.body is None:
        raise OneNoteError("Graph HTML has no body")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    for image in soup.find_all("img"):
        alt = image.get("alt", "")
        reference = image.get("data-fullres-src") or image.get("src", "")
        image.replace_with(f"Image: {alt} ({reference})")
    for obj in soup.find_all("object"):
        obj.replace_with(f"Attachment: {obj.get('data-attachment', '')} ({obj.get('data', '')})")
    for anchor in soup.find_all("a", href=True):
        anchor.append(f" ({anchor['href']})")
    return soup.get_text(separator="\n", strip=True)


def _row(page: dict, breadcrumb: list[str], body: str) -> dict:
    _require(page, "id", "title", "lastModifiedDateTime")
    links = page.get("links", {})
    url = links.get("oneNoteWebUrl", {}).get("href", "")
    if not isinstance(url, str):
        raise OneNoteError("Invalid page source URL")
    return {
        "id": page["id"],
        "title": page["title"],
        "url": url,
        "content": f"OneNote: {' > '.join(breadcrumb)}\nSource: {url}\n\n{body}",
    }


def _snapshot(graph, selected, previous, max_pages, max_cache_bytes):
    cache = copy.deepcopy(previous)
    cache.setdefault("bodies", {})
    cache.setdefault("notebooks", {})
    notebooks = {n["id"]: n for n in graph.collection("notebooks")}
    rows = {notebook_id: {} for notebook_id in selected}
    locations = {}
    deleted_parents = set()

    def children(path):
        try:
            return list(graph.collection(path))
        except OneNoteError as exc:
            if exc.status in (404, 410) and exc.code == "20113":
                return []
            raise

    def walk(container, prefix, notebook_id, ancestors):
        if container in ancestors or len(ancestors) > 30:
            raise OneNoteError("Section group cycle or excessive nesting")
        for section in children(f"{container}/sections"):
            _require(section, "id", "displayName")
            for page in children(f"sections/{_path(section['id'])}/pages"):
                _require(page, "id", "title", "lastModifiedDateTime")
                if page["id"] in locations:
                    raise OneNoteError("Page appeared in multiple locations during traversal")
                locations[page["id"]] = (notebook_id, prefix + [section["displayName"]], page)
                if len(locations) > max_pages:
                    raise OneNoteError("Page limit exceeded; increase max_pages explicitly")
        for group in children(f"{container}/sectionGroups"):
            _require(group, "id", "displayName")
            walk(
                f"sectionGroups/{_path(group['id'])}",
                prefix + [group["displayName"]],
                notebook_id,
                ancestors | {container},
            )

    for notebook_id in selected:
        notebook = notebooks.get(notebook_id)
        if notebook is None:
            try:
                notebook = graph.request(f"{_BASE}/notebooks/{_path(notebook_id)}")
            except OneNoteError as exc:
                if exc.status not in (404, 410) or exc.code != "20113":
                    raise
                deleted_parents.add(notebook_id)
                continue
        _require(notebook, "id", "displayName")
        if notebook["id"] != notebook_id:
            raise OneNoteError("Notebook identity mismatch")
        walk(f"notebooks/{_path(notebook_id)}", [notebook["displayName"]], notebook_id, set())

    for notebook_id in selected:
        old_rows = cache["notebooks"].get(notebook_id, {})
        for page_id, old_row in old_rows.items():
            if page_id in locations:
                continue
            try:
                page = graph.request(
                    f"{_BASE}/pages/{_path(page_id)}?$expand=parentNotebook,parentSection"
                )
            except OneNoteError as exc:
                if exc.status in (404, 410) and (
                    exc.code == "20113"
                    or (exc.code == "20102" and notebook_id not in deleted_parents)
                ):
                    continue
                raise
            _require(page, "id", "title", "lastModifiedDateTime")
            if page["id"] != page_id:
                raise OneNoteError("Page identity mismatch")
            parent = page.get("parentNotebook", {})
            _require(parent, "id")
            if parent["id"] in selected:
                # A selected move not seen in the walk means the listing was unstable.
                raise OneNoteError("Page moved during traversal; retry a fresh complete snapshot")
            rows[notebook_id][page_id] = old_row
            logger.warning("Retaining a previously ingested page moved outside selected notebooks")

    for page_id, (notebook_id, breadcrumb, page) in locations.items():
        body = cache["bodies"].get(page_id)
        if (
            body is None
            or body.get("timestamp") != page["lastModifiedDateTime"]
            or body.get("renderer") != _RENDERER_VERSION
        ):
            body = {
                "timestamp": page["lastModifiedDateTime"],
                "renderer": _RENDERER_VERSION,
                "text": _body(graph.request(f"{_BASE}/pages/{_path(page_id)}/content", html=True)),
            }
            cache["bodies"][page_id] = body
        rows[notebook_id][page_id] = _row(page, breadcrumb, body["text"])
    cache["notebooks"].update(rows)
    retained = {page_id for values in cache["notebooks"].values() for page_id in values}
    cache["bodies"] = {key: value for key, value in cache["bodies"].items() if key in retained}
    if len(retained) > max_pages or len(json.dumps(cache).encode()) > max_cache_bytes:
        raise OneNoteError("Retained cache limit exceeded; increase limits explicitly")
    return rows, cache


def onenote_source(
    token: Token,
    *,
    notebook_ids: list[str],
    account_id: str,
    http_client: httpx.Client | None = None,
    max_pages: int = 1000,
    max_cache_bytes: int = 16777216,
):
    """Create scoped document resources. Use a stable account-and-tenant identity.

    Only one active sync per account/dataset is supported. Deselecting a notebook
    retains its table and memory. Cache state belongs to the source, because DLT
    resets resource state when extracting resources with replace disposition.
    """
    if not account_id.strip() or not notebook_ids or any(not n.strip() for n in notebook_ids):
        raise ValueError("Stable account_id and explicit nonempty notebook_ids are required")
    if max_pages <= 0 or max_cache_bytes <= 0:
        raise ValueError("Cache limits must be positive")
    selected = sorted(set(notebook_ids))
    source_name = f"onenote_{_identity(account_id)}"
    prepared = None
    source = None

    def records(notebook_id):
        nonlocal prepared
        if prepared is None:
            state = dlt.current.source_state()
            graph = _Graph(token, http_client)
            try:
                prepared, cache = _snapshot(
                    graph, selected, state.get("onenote_cache", {}), max_pages, max_cache_bytes
                )
            finally:
                graph.close()
            state["onenote_cache"] = cache
            source._onenote_extracted = True
        notebook_rows = prepared[notebook_id]
        if notebook_rows:
            yield from notebook_rows.values()
        else:
            yield dlt.mark.materialize_table_schema()

    @dlt.source(name=source_name)
    def selected_notebooks():
        return [
            dlt.resource(
                records(notebook_id),
                name=f"onenote_{_identity(account_id + ':' + notebook_id)}",
                primary_key="id",
                write_disposition="replace",
                columns={
                    field: {"data_type": "text", "nullable": False}
                    for field in ("id", "title", "content", "url")
                },
            )
            for notebook_id in selected
        ]

    source = selected_notebooks()
    setattr(source, DOCUMENT_SOURCE_ATTR, source_name)
    setattr(source, PIPELINE_SCOPE_ATTR, source_name)
    source._onenote_extracted = False
    return source
