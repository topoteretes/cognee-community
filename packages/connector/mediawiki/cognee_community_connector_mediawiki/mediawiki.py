"""MediaWiki data-source connector for cognee.

The connector exposes selected wiki pages as a document-mode ``dlt`` resource.
It performs a scoped initial load, then consumes MediaWiki's ``recentchanges``
feed for incremental updates and hard-delete tombstones.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("mediawiki_connector")

MEDIAWIKI_SOURCE_NAME = "mediawiki"
MEDIAWIKI_TABLE_NAME = "mediawiki_pages"

_CURSOR_KEY = "recentchanges_timestamp"
_SEEN_RCIDS_KEY = "recentchanges_seen_rcids"
_DEFAULT_OVERLAP_SECONDS = 10
_MAX_SEEN_RCIDS = 5000
_MAX_RETRIES = 4
_DEFAULT_USER_AGENT = "cognee-community-mediawiki-connector/0.1"


@dataclass(frozen=True)
class _MediaWikiConfig:
    api_url: str
    page_titles: tuple[str, ...]
    page_title_keys: frozenset[str]
    page_prefix: str | None
    page_prefix_key: str | None
    namespaces: frozenset[int]
    timeout: float
    overlap_seconds: int


def mediawiki_source(
    api_url: str | None = None,
    *,
    page_titles: list[str] | tuple[str, ...] | None = None,
    page_prefix: str | None = None,
    namespaces: list[int] | tuple[int, ...] = (0,),
    user_agent: str = _DEFAULT_USER_AGENT,
    timeout: float = 30.0,
    overlap_seconds: int = _DEFAULT_OVERLAP_SECONDS,
    client: Any = None,
):
    """Return a ``dlt`` resource for selected MediaWiki pages.

    Args:
        api_url: URL of the wiki's ``api.php`` endpoint. Falls back to
            ``MEDIAWIKI_API_URL``.
        page_titles: Exact page titles to ingest.
        page_prefix: Prefix used with ``list=allpages`` during the initial load.
        namespaces: Namespace IDs included by ``page_prefix`` and incremental
            changes. The default is the main namespace (0).
        user_agent: HTTP User-Agent sent to the wiki.
        timeout: Per-request timeout in seconds.
        overlap_seconds: Recent-changes overlap used to catch slightly
            out-of-order events.
        client: Optional preconfigured ``httpx.Client``-compatible client. Use
            this injection point for authenticated private wikis and tests.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            "The MediaWiki connector requires dlt. Install this package's dependencies first."
        ) from exc

    config = _build_config(
        api_url=api_url,
        page_titles=page_titles,
        page_prefix=page_prefix,
        namespaces=namespaces,
        timeout=timeout,
        overlap_seconds=overlap_seconds,
    )

    @dlt.resource(
        name=MEDIAWIKI_TABLE_NAME,
        write_disposition="merge",
        primary_key="id",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def mediawiki_pages():
        owns_client = client is None
        http_client = client or _build_http_client(user_agent=user_agent, timeout=timeout)
        try:
            yield from _iter_rows(http_client, config, dlt.current.resource_state())
        finally:
            if owns_client:
                http_client.close()

    resource = mediawiki_pages()
    setattr(resource, DOCUMENT_SOURCE_ATTR, MEDIAWIKI_SOURCE_NAME)
    return resource


def _build_config(
    *,
    api_url: str | None,
    page_titles: list[str] | tuple[str, ...] | None,
    page_prefix: str | None,
    namespaces: list[int] | tuple[int, ...],
    timeout: float,
    overlap_seconds: int,
) -> _MediaWikiConfig:
    resolved_api_url = (api_url or os.getenv("MEDIAWIKI_API_URL") or "").strip()
    parsed_url = urlparse(resolved_api_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ValueError(
            "api_url must be a valid HTTP(S) MediaWiki api.php URL, or set MEDIAWIKI_API_URL."
        )

    if isinstance(page_titles, str):
        raise TypeError("page_titles must be a sequence of titles, not a single string.")
    cleaned_titles = tuple(
        dict.fromkeys(title.strip() for title in (page_titles or ()) if title and title.strip())
    )
    cleaned_prefix = page_prefix.strip() if page_prefix is not None else None
    if cleaned_prefix == "":
        raise ValueError("page_prefix cannot be empty; use an explicit prefix or page_titles.")
    if not cleaned_titles and cleaned_prefix is None:
        raise ValueError(
            "Select pages with page_titles or page_prefix to avoid an unbounded wiki sync."
        )

    namespace_ids = frozenset(int(namespace) for namespace in namespaces)
    if cleaned_prefix is not None and not namespace_ids:
        raise ValueError("namespaces cannot be empty when page_prefix is used.")
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero.")
    if overlap_seconds < 0:
        raise ValueError("overlap_seconds cannot be negative.")

    return _MediaWikiConfig(
        api_url=resolved_api_url,
        page_titles=cleaned_titles,
        page_title_keys=frozenset(_title_key(title) for title in cleaned_titles),
        page_prefix=cleaned_prefix,
        page_prefix_key=_title_key(cleaned_prefix) if cleaned_prefix is not None else None,
        namespaces=namespace_ids,
        timeout=float(timeout),
        overlap_seconds=overlap_seconds,
    )


def _build_http_client(*, user_agent: str, timeout: float):
    try:
        import httpx
    except ImportError as exc:
        raise ImportError(
            "The MediaWiki connector requires httpx. Install this package's dependencies first."
        ) from exc

    return httpx.Client(
        headers={"User-Agent": user_agent},
        follow_redirects=True,
        timeout=timeout,
    )


def _iter_rows(client: Any, config: _MediaWikiConfig, state: dict) -> Iterator[dict]:
    """Yield initial page rows or incremental changes and update cursor state."""
    cursor_value = state.get(_CURSOR_KEY)
    if cursor_value is None:
        baseline = _server_timestamp(client, config)
        seen_page_ids: set[str] = set()
        count = 0
        for selector, value in _iter_initial_selectors(client, config):
            page = _fetch_page(client, config, selector=selector, value=value)
            if page is None:
                continue
            page_id = str(page["pageid"])
            if page_id in seen_page_ids or not _page_in_scope(page, config):
                continue
            seen_page_ids.add(page_id)
            count += 1
            yield _page_to_row(page)

        state[_CURSOR_KEY] = baseline
        state[_SEEN_RCIDS_KEY] = []
        logger.info("MediaWiki: initial sync yielded %d page(s).", count)
        return

    previous = _parse_timestamp(cursor_value)
    upper = _parse_timestamp(_server_timestamp(client, config))
    if upper < previous:
        raise RuntimeError(
            "MediaWiki server timestamp moved backwards; refusing to advance the sync cursor."
        )

    overlap = timedelta(seconds=config.overlap_seconds)
    lower = previous - overlap
    tail_boundary = upper - overlap
    previously_seen = {int(rcid) for rcid in state.get(_SEEN_RCIDS_KEY, [])}
    tail_rcids: set[int] = set()
    actions: dict[str, str] = {}

    for change in _iter_recent_changes(client, config, lower=lower, upper=upper):
        rcid = int(change.get("rcid") or 0)
        timestamp = _parse_timestamp(change["timestamp"])
        if rcid and timestamp >= tail_boundary:
            tail_rcids.add(rcid)
        if rcid and rcid in previously_seen:
            continue

        action = _classify_change(change, config)
        if action is not None:
            page_id, operation = action
            actions[page_id] = operation

    updated = 0
    deleted = 0
    for page_id in sorted(actions, key=_page_id_sort_key):
        operation = actions[page_id]
        if operation == "delete":
            deleted += 1
            yield _deleted_row(page_id)
            continue

        page = _fetch_page(client, config, selector="pageids", value=page_id)
        if page is None or not _page_in_scope(page, config):
            deleted += 1
            yield _deleted_row(page_id)
            continue

        updated += 1
        yield _page_to_row(page)

    state[_CURSOR_KEY] = _format_timestamp(upper)
    state[_SEEN_RCIDS_KEY] = sorted(tail_rcids)[-_MAX_SEEN_RCIDS:]
    logger.info(
        "MediaWiki: incremental sync yielded %d update(s), %d deletion(s).",
        updated,
        deleted,
    )


def _iter_initial_selectors(client: Any, config: _MediaWikiConfig) -> Iterator[tuple[str, str]]:
    for title in config.page_titles:
        yield "titles", title

    if config.page_prefix is None:
        return
    for namespace in sorted(config.namespaces):
        continuation: dict[str, Any] = {}
        while True:
            params: dict[str, Any] = {
                "list": "allpages",
                "apprefix": config.page_prefix,
                "apnamespace": namespace,
                "aplimit": "max",
            }
            params.update(continuation)
            payload = _api_get(client, config, params)
            for page in payload.get("query", {}).get("allpages", []):
                if page.get("pageid") is not None:
                    yield "pageids", str(page["pageid"])
            continuation = payload.get("continue") or {}
            if not continuation:
                break


def _fetch_page(
    client: Any,
    config: _MediaWikiConfig,
    *,
    selector: str,
    value: str,
) -> dict | None:
    """Fetch one page's latest revision and all category memberships."""
    if selector not in {"pageids", "titles"}:
        raise ValueError(f"Unsupported MediaWiki page selector: {selector}")

    continuation: dict[str, Any] = {}
    page_data: dict[str, Any] | None = None
    categories: dict[str, dict] = {}
    while True:
        params: dict[str, Any] = {
            selector: value,
            "prop": "info|revisions|categories",
            "inprop": "url",
            "rvprop": "ids|timestamp|user|comment|content",
            "rvslots": "main",
            "rvlimit": 1,
            "cllimit": "max",
        }
        params.update(continuation)
        payload = _api_get(client, config, params)
        pages = payload.get("query", {}).get("pages", [])
        if not pages:
            return None
        current = pages[0]
        if current.get("missing") is not None or current.get("invalid") is not None:
            return None

        if page_data is None:
            page_data = dict(current)
            page_data["categories"] = []
        for category in current.get("categories", []):
            title = category.get("title")
            if title:
                categories[title] = category

        continuation = payload.get("continue") or {}
        if not continuation:
            break

    if page_data is not None:
        page_data["categories"] = [categories[key] for key in sorted(categories)]
    return page_data


def _page_to_row(page: dict[str, Any]) -> dict[str, Any]:
    revisions = page.get("revisions") or []
    revision = revisions[0] if revisions else {}
    slot = (revision.get("slots") or {}).get("main") or {}
    wikitext = slot.get("content") or slot.get("*") or revision.get("*") or ""
    content = _wikitext_to_plain_text(wikitext)
    title = str(page.get("title") or "")

    category_names = []
    for category in page.get("categories") or []:
        category_title = str(category.get("title") or "")
        if category_title.startswith("Category:"):
            category_title = category_title[len("Category:") :]
        if category_title:
            category_names.append(category_title)

    return {
        "id": str(page["pageid"]),
        "title": title,
        "content": content or title,
        "url": page.get("fullurl"),
        "revision_id": revision.get("revid"),
        "revision_parent_id": revision.get("parentid"),
        "revision_timestamp": revision.get("timestamp"),
        "revision_user": revision.get("user"),
        "revision_comment": revision.get("comment"),
        "categories": sorted(set(category_names)),
        "_deleted": False,
    }


def _wikitext_to_plain_text(wikitext: str) -> str:
    try:
        import mwparserfromhell
    except ImportError as exc:
        raise ImportError(
            "The MediaWiki connector requires mwparserfromhell. "
            "Install this package's dependencies first."
        ) from exc

    return str(mwparserfromhell.parse(wikitext).strip_code(normalize=True, collapse=True)).strip()


def _iter_recent_changes(
    client: Any,
    config: _MediaWikiConfig,
    *,
    lower: datetime,
    upper: datetime,
) -> Iterator[dict]:
    continuation: dict[str, Any] = {}
    while True:
        params: dict[str, Any] = {
            "list": "recentchanges",
            "rcstart": _format_timestamp(lower),
            "rcend": _format_timestamp(upper),
            "rcdir": "newer",
            "rctype": "edit|new|log|categorize",
            "rcprop": "title|ids|timestamp|loginfo",
            "rclimit": "max",
        }
        params.update(continuation)
        payload = _api_get(client, config, params)
        yield from payload.get("query", {}).get("recentchanges", [])
        continuation = payload.get("continue") or {}
        if not continuation:
            break


def _classify_change(change: dict[str, Any], config: _MediaWikiConfig) -> tuple[str, str] | None:
    page_id = str(change.get("pageid") or "")
    if not page_id or page_id == "0":
        return None

    title = str(change.get("title") or "")
    namespace = int(change.get("ns") or 0)
    change_type = change.get("type")
    log_type = change.get("logtype")
    log_action = change.get("logaction")

    if log_type == "move" and log_action == "move":
        target_title = str((change.get("logparams") or {}).get("target_title") or "")
        target_namespace = int((change.get("logparams") or {}).get("target_ns") or namespace)
        if _in_scope(target_title, target_namespace, config):
            return page_id, "upsert"
        if _in_scope(title, namespace, config):
            return page_id, "delete"
        return None

    if not _in_scope(title, namespace, config):
        return None
    if log_type == "delete" and log_action in {"delete", "delete_redir"}:
        return page_id, "delete"
    if change_type in {"edit", "new", "categorize"}:
        return page_id, "upsert"
    if log_type == "delete" and log_action in {"restore", "undelete"}:
        return page_id, "upsert"
    return None


def _page_in_scope(page: dict[str, Any], config: _MediaWikiConfig) -> bool:
    return _in_scope(str(page.get("title") or ""), int(page.get("ns") or 0), config)


def _in_scope(title: str, namespace: int, config: _MediaWikiConfig) -> bool:
    key = _title_key(title)
    if key in config.page_title_keys:
        return True
    return (
        config.page_prefix_key is not None
        and namespace in config.namespaces
        and key.startswith(config.page_prefix_key)
    )


def _title_key(title: str) -> str:
    return " ".join(str(title).replace("_", " ").split()).casefold()


def _deleted_row(page_id: str) -> dict[str, Any]:
    return {"id": str(page_id), "_deleted": True}


def _page_id_sort_key(page_id: str) -> tuple[int, int | str]:
    try:
        return 0, int(page_id)
    except ValueError:
        return 1, page_id


def _server_timestamp(client: Any, config: _MediaWikiConfig) -> str:
    payload = _api_get(client, config, {"curtimestamp": 1})
    timestamp = payload.get("curtimestamp")
    if not timestamp:
        raise RuntimeError("MediaWiki response did not include curtimestamp.")
    _parse_timestamp(timestamp)
    return timestamp


def _api_get(client: Any, config: _MediaWikiConfig, params: dict[str, Any]) -> dict:
    request_params = {
        "action": "query",
        "format": "json",
        "formatversion": 2,
        "maxlag": 5,
        **params,
    }

    for attempt in range(_MAX_RETRIES):
        try:
            response = client.get(
                config.api_url,
                params=request_params,
                timeout=config.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            if attempt == _MAX_RETRIES - 1 or not _is_transient_http_error(exc):
                raise RuntimeError(f"MediaWiki request failed: {exc}") from exc
            time.sleep(_retry_delay(getattr(exc, "response", None), attempt))
            continue

        error = payload.get("error")
        if error is None:
            return payload
        if error.get("code") == "maxlag" and attempt < _MAX_RETRIES - 1:
            time.sleep(float(error.get("lag") or 2**attempt))
            continue
        raise RuntimeError(
            f"MediaWiki API error {error.get('code', 'unknown')}: {error.get('info', 'no details')}"
        )

    raise RuntimeError("MediaWiki request exhausted its retry budget.")


def _is_transient_http_error(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code == 429 or (isinstance(status_code, int) and status_code >= 500):
        return True
    try:
        import httpx

        return isinstance(exc, httpx.TransportError)
    except ImportError:
        return False


def _retry_delay(response: Any, attempt: int) -> float:
    headers = getattr(response, "headers", {}) or {}
    retry_after = headers.get("Retry-After") or headers.get("retry-after")
    try:
        return float(retry_after)
    except (TypeError, ValueError):
        return float(2**attempt)


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Invalid MediaWiki timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
