"""Complete selected snapshots with incremental HTML fetching and conservative deletion."""

import hashlib
import json
import math
import time
from collections.abc import Callable
from urllib.parse import quote, urlsplit

import dlt
import httpx
from bs4 import BeautifulSoup
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR
from dlt.common.normalizers.naming.snake_case import NamingConvention

_BASE = "https://graph.microsoft.com/v1.0/me/onenote"
_RENDERER_VERSION = 1
logger = get_logger(__name__)
Token = str | Callable[[], str]


class OneNoteError(RuntimeError):
    """Graph failure with safe diagnostics (never response bodies or bearer tokens)."""

    def __init__(self, message: str, *, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


class _CallbackError(RuntimeError):
    """Host callback failure, distinct from a checkpointable Graph failure."""


def _invoke_callback(callback):
    try:
        return callback()
    except Exception as exc:
        raise _CallbackError(str(exc)) from exc


def _identity(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def _path(value: str) -> str:
    return quote(value, safe="")


class _Graph:
    def __init__(
        self,
        token: Token,
        client: httpx.Client | None,
        *,
        stats=None,
        check_active=None,
        max_requests=300,
    ):
        self.token = token
        self.client = client or httpx.Client()
        self.owned = client is None
        self.stats = stats if stats is not None else {"requests": 0}
        self.check_active = check_active
        self.max_requests = max_requests

    def ensure_active(self):
        if self.check_active is not None:
            _invoke_callback(self.check_active)

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
        slept = 0
        for attempt in range(5):
            self.ensure_active()
            if self.stats["requests"] >= self.max_requests:
                raise OneNoteError("Graph request budget exhausted", code="budget")
            token = _invoke_callback(self.token) if callable(self.token) else self.token
            if not isinstance(token, str) or not token.strip():
                raise OneNoteError("A delegated access token is required")
            self.stats["requests"] += 1
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
                delay = 2**attempt
                if slept + delay > 60:
                    raise OneNoteError("Graph retry wait budget exhausted") from None
                slept += delay
                time.sleep(delay)
                continue
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 4:
                try:
                    delay = float(response.headers.get("Retry-After", 2**attempt))
                    if not math.isfinite(delay) or delay < 0:
                        delay = 2**attempt
                except ValueError:
                    delay = 2**attempt
                if slept + delay > 60:
                    raise OneNoteError(
                        "Graph Retry-After exceeds wait budget", status=response.status_code
                    )
                slept += delay
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
        url = f"{base}?$orderby=createdDateTime%20asc&$top=100&$skip=0"
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
                url = f"{base}?$orderby=createdDateTime%20asc&$top=100&$skip={offset}"
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
    if not isinstance(links, dict) or not isinstance(links.get("oneNoteWebUrl", {}), dict):
        raise OneNoteError("Invalid page source links")
    url = links.get("oneNoteWebUrl", {}).get("href", "")
    if not isinstance(url, str):
        raise OneNoteError("Invalid page source URL")
    return {
        "id": page["id"],
        "title": page["title"],
        "url": url,
        "content": f"OneNote: {' > '.join(breadcrumb)}\nSource: {url}\n\n{body}",
    }


def _snapshot(graph, notebook_id, cache):
    """Build an authoritative view; retain parsed bodies even if traversal fails."""
    cache.setdefault("bodies", {})
    old_ids = set(cache.get("page_ids", []))
    rows = {}
    locations = {}
    deleted_parent = False

    def children(path):
        try:
            items = []
            for child in graph.collection(path):
                # Observed reappearance invalidates old removal evidence even if
                # a later page, pagination batch or container makes this run fail.
                if path.endswith("/pages"):
                    cache.setdefault("confirmed_missing", {}).pop(child["id"], None)
                items.append(child)
            return items
        except OneNoteError as exc:
            if exc.status in (404, 410) and exc.code == "20113":
                return []
            raise

    notebooks = {n["id"]: n for n in graph.collection("notebooks")}
    notebook = notebooks.get(notebook_id)
    if notebook is None:
        try:
            notebook = graph.request(f"{_BASE}/notebooks/{_path(notebook_id)}")
        except OneNoteError as exc:
            if exc.status not in (404, 410) or exc.code != "20113":
                raise
            deleted_parent = True
    if not deleted_parent:
        _require(notebook, "id", "displayName")
        if notebook["id"] != notebook_id:
            raise OneNoteError("Notebook identity mismatch")
        pending = [(f"notebooks/{_path(notebook_id)}", [notebook["displayName"]])]
        seen = set()
        while pending:
            container, prefix = pending.pop()
            if container in seen:
                raise OneNoteError("Section group cycle")
            seen.add(container)
            for section in children(f"{container}/sections"):
                _require(section, "id", "displayName")
                for page in children(f"sections/{_path(section['id'])}/pages"):
                    _require(page, "id", "title", "lastModifiedDateTime")
                    page_id = page["id"]
                    if page_id in locations:
                        raise OneNoteError("Page appeared in multiple locations during traversal")
                    locations[page_id] = (prefix + [section["displayName"]], page)
                    graph.stats["scanned"] += 1
                    body = cache["bodies"].get(page_id)
                    if (
                        body is None
                        or body.get("timestamp") != page["lastModifiedDateTime"]
                        or body.get("renderer") != _RENDERER_VERSION
                    ):
                        body = {
                            "timestamp": page["lastModifiedDateTime"],
                            "renderer": _RENDERER_VERSION,
                            "text": _body(
                                graph.request(f"{_BASE}/pages/{_path(page_id)}/content", html=True)
                            ),
                        }
                        cache["bodies"][page_id] = body
                    else:
                        graph.stats["skipped"] += 1
                    rows[page_id] = _row(page, locations[page_id][0], body["text"])
            for group in children(f"{container}/sectionGroups"):
                _require(group, "id", "displayName")
                pending.append(
                    (f"sectionGroups/{_path(group['id'])}", prefix + [group["displayName"]])
                )

    # Only a fresh complete walk can validate these checkpoints. A page restored
    # to this notebook must be confirmed again if it disappears on a later run.
    missing = old_ids - set(rows)
    confirmed = cache.setdefault("confirmed_missing", {})
    for page_id in set(confirmed) - missing:
        del confirmed[page_id]
    for page_id in sorted(missing - set(confirmed)):
        try:
            page = graph.request(
                f"{_BASE}/pages/{_path(page_id)}?$expand=parentNotebook,parentSection"
            )
        except OneNoteError as exc:
            # Either the full accessible traversal or known parent deletion supplies
            # the surrounding evidence required for a nonexistent page (20102).
            if exc.status in (404, 410) and exc.code in ("20113", "20102"):
                confirmed[page_id] = True
                continue
            raise
        _require(page, "id", "title", "lastModifiedDateTime")
        if page["id"] != page_id:
            raise OneNoteError("Page identity mismatch")
        parent = page.get("parentNotebook", {})
        _require(parent, "id")
        if parent["id"] == notebook_id:
            raise OneNoteError("Page moved during traversal; retry a fresh complete snapshot")
        # A positive move outside this notebook authorizes removing its old copy.
        confirmed[page_id] = True
    return rows


def onenote_source(
    token: Token,
    *,
    notebook_id: str,
    account_id: str,
    resource_name: str | None = None,
    http_client: httpx.Client | None = None,
    service: httpx.Client | None = None,
    check_active: Callable[[], None] | None = None,
    max_requests: int = 300,
):
    """Return one merge resource with durable body checkpoints and scoped deletion.

    Expected Graph failures (including malformed page metadata), throttling and
    budget exhaustion return normally so DLT can persist parsed bodies and
    confirmed removals. No document changes are published on these runs. A normal
    return is not proof of a completed sync: require ``cognee_sync_stats['failed']
    == 0`` and ``cognee_sync_stats['complete'] == 1`` before processing memory.
    A malformed page blocks its entire notebook until corrected. Callback failures
    and unexpected programming errors raise, preserving their original cause.

    ``max_requests=300`` is a conservative per-extraction choice below Microsoft's
    delegated allowance of 400 requests/hour, not an hourly rate limiter. It
    counts metadata, HTML, removal checks and every retry. Confirmed removals
    resume across runs, but if metadata enumeration alone exhausts this budget,
    increasing it or reducing the notebook structure is required. The cumulative
    60-second retry sleep budget per request bounds waiting while Cognee holds its
    staging lock; a longer server delay stops the request without retrying early.

    Only one active sync per account/dataset is supported. Omitted notebooks retain
    their memory. ``resource_name`` is normalized to DLT's snake-case table name
    and must not be empty or whitespace; preserve that canonical name across runs.
    """
    # Core version 1 introduced table-scoped cleanup and confirmed empty tables;
    # later versions add capabilities this connector does not require.
    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError("OneNote requires Cognee table-scoped document sync support")
    if not account_id.strip() or not notebook_id.strip():
        raise ValueError("Stable account_id and explicit notebook_id are required")
    if http_client is not None and service is not None:
        raise ValueError("Supply only one of http_client and service")
    if not isinstance(max_requests, int) or isinstance(max_requests, bool) or max_requests <= 0:
        raise ValueError("max_requests must be a positive integer")
    if resource_name is not None and (
        not isinstance(resource_name, str) or not resource_name.strip()
    ):
        raise ValueError("resource_name must be a nonempty, non-whitespace string")
    identity = _identity(json.dumps([account_id, notebook_id], ensure_ascii=True))
    name = NamingConvention().normalize_table_identifier(
        resource_name if resource_name is not None else f"onenote_{identity}"
    )
    stats: dict[str, int] = {}

    @dlt.resource(
        name=name,
        primary_key="id",
        write_disposition="merge",
        columns={
            **{
                field: {"data_type": "text", "nullable": False}
                for field in ("id", "title", "content", "url")
            },
            "_deleted": {"data_type": "bool", "hard_delete": True},
        },
    )
    def records():
        # Pipeline-level hints may be applied to a cloned resource. Inspect the
        # extraction source as well as this public resource before any I/O.
        current = dlt.current.source().resources[name]
        if current.write_disposition != "merge" or resource.write_disposition != "merge":
            raise ValueError("OneNote requires write_disposition='merge'")
        stats.clear()
        stats.update(
            dict.fromkeys(
                (
                    "scanned",
                    "skipped",
                    "failed",
                    "deleted",
                    "failed_rate_limit",
                    "failed_budget",
                    "requests",
                    "complete",
                ),
                0,
            )
        )
        state = dlt.current.resource_state()
        if state.get("connection", identity) != identity:
            raise ValueError("Resource name is already bound to another OneNote connection")
        state["connection"] = identity
        cache = state.setdefault("onenote_cache", {})
        if "rows" in cache:
            # Earlier development builds duplicated body text in rendered rows.
            cache["page_ids"] = list(cache.pop("rows"))
        graph = _Graph(
            token,
            http_client if http_client is not None else service,
            stats=stats,
            check_active=check_active,
            max_requests=max_requests,
        )
        try:
            try:
                rows = _snapshot(graph, notebook_id, cache)
            except OneNoteError as exc:
                stats["failed"] += 1
                stats["failed_rate_limit"] += int(exc.status == 429)
                stats["failed_budget"] += int(exc.code == "budget")
                logger.warning(
                    "OneNote sync incomplete; retaining documents and checkpointing bodies",
                    reason=str(exc),
                )
                yield dlt.mark.materialize_table_schema()
                return
            graph.ensure_active()
            deleted = set(cache.get("page_ids", [])) - set(rows)
            for page_id in sorted(deleted):
                graph.ensure_active()
                yield {"id": page_id, "title": "", "content": "", "url": "", "_deleted": True}
            if rows:
                for page_id in sorted(rows):
                    graph.ensure_active()
                    yield rows[page_id]
            elif not deleted:
                yield dlt.mark.materialize_table_schema()
            cache["page_ids"] = sorted(rows)
            cache["bodies"] = {
                key: value for key, value in cache.get("bodies", {}).items() if key in rows
            }
            cache.pop("confirmed_missing", None)
            stats["deleted"] = len(deleted)
            stats["complete"] = 1
        finally:
            graph.close()

    resource = records()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "onenote")
    setattr(resource, PIPELINE_SCOPE_ATTR, name)
    resource.cognee_sync_stats = stats
    return resource
