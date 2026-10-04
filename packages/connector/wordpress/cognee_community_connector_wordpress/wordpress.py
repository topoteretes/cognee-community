"""WordPress connector for cognee — a ``dlt`` source that turns WordPress content into memory.

Sync WordPress posts, pages, comments, and configurable custom post types into cognee,
incrementally and with forget-on-deletion — "ask my WordPress site".

Built on cognee's DLT ingestion subsystem and document-mode routing; the resource
produced here is handed directly to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_wordpress import wordpress_source

    await cognee.remember(
        wordpress_source(
            base_url="https://example.com",
            username="admin",
            app_password="xxxx xxxx xxxx xxxx",
            content_types=["posts", "pages", "comments"],
        ),
        dataset_name="my_wordpress",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

Design
------
* **Auth** — WordPress Application Passwords (WordPress 5.6+). Pass ``username`` and
  ``app_password`` (or set ``WORDPRESS_USERNAME`` and ``WORDPRESS_APP_PASSWORD``).
  They are transmitted over HTTP Basic Auth. Access is read-only — only ``GET`` requests
  are sent.
* **Scope** — Defaults to ``["posts", "pages", "comments"]``. Additional custom post types
  (e.g., ``["products", "documentation"]``) can be supplied via ``custom_post_types`` or
  in ``content_types``. Works on both self-hosted sites and WordPress.com.
* **Primary Key** — ``{content_type}:{item_id}`` (e.g. ``posts:42``). Combined with
  ``write_disposition="merge"``, this gives idempotent upserts without ID collisions across
  types.
* **Incremental Cursor** — Emits only records modified since the previous sync using the
  WordPress REST API ``modified_after`` filter (and ``after`` for comments). The cursor
  is stored in dlt's per-resource state.
* **Forget-on-Delete** — Each run conducts a lightweight ID sweep (requesting only item IDs)
  and compares against the IDs recorded on the prior run. Vanished items are emitted with
  the ``_deleted`` hard-delete marker; dlt removes those rows on ``merge`` and cognee's
  ``orphan_cleanup`` purges them from the graph and vector stores.
* **Document Mode** — Declares ``cognee_document_source = "wordpress"`` via
  ``DOCUMENT_SOURCE_ATTR``, routing content as documents through normal cognify.
"""

from __future__ import annotations

import html
import os
import re
import time
from collections.abc import Iterator
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("wordpress_connector")

WORDPRESS_TABLE_NAME = "wordpress_content"
WORDPRESS_SOURCE_NAME = "wordpress"
DEFAULT_CONTENT_TYPES = ["posts", "pages", "comments"]
_MAX_RETRIES = 5

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_WS_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Auth & URL helpers
# ---------------------------------------------------------------------------
def _normalize_api_url(base_url: str) -> str:
    """Normalize base URL to point to the WordPress REST API v2 root.

    Handles self-hosted WordPress (`https://example.com/wp-json/wp/v2`) and
    WordPress.com REST API endpoints.
    """
    url = base_url.strip().rstrip("/")
    if "/wp/v2" in url:
        return url
    if "/wp-json" in url:
        return f"{url}/wp/v2"
    if "wordpress.com" in url and "public-api.wordpress.com" not in url:
        # e.g. https://sitename.wordpress.com -> https://public-api.wordpress.com/wp/v2/sites/sitename.wordpress.com
        site_domain = url.split("://")[-1].rstrip("/")
        return f"https://public-api.wordpress.com/wp/v2/sites/{site_domain}"
    return f"{url}/wp-json/wp/v2"


def _make_session(username: str | None = None, app_password: str | None = None) -> Any:
    """Build an authenticated ``requests.Session`` using HTTP Basic Auth."""
    try:
        import requests
    except ImportError as exc:
        raise ImportError(
            'The WordPress connector requires "requests". Install it via:\n'
            '    pip install "requests"'
        ) from exc

    session = requests.Session()
    if username and app_password:
        # WordPress application passwords may contain spaces (e.g. 'xxxx xxxx xxxx xxxx')
        session.auth = (username, app_password.replace(" ", ""))
    session.headers.update(
        {"Accept": "application/json", "User-Agent": "cognee-wordpress-connector/0.1.0"}
    )
    return session


# ---------------------------------------------------------------------------
# API request & pagination helpers
# ---------------------------------------------------------------------------
def _is_transient(exc: Exception) -> bool:
    """Check if exception is transient and eligible for retry."""
    import requests

    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return False


def _retry_after(response: Any | None, attempt: int) -> float:
    """Determine seconds to wait before retrying."""
    if response is not None:
        header = response.headers.get("Retry-After")
        if header:
            try:
                return float(header)
            except (ValueError, TypeError):
                pass
    return float(2**attempt)


def _api_get(session: Any, url: str, params: dict | None = None) -> Any:
    """GET a WordPress API endpoint with backoff for transient errors."""
    for attempt in range(_MAX_RETRIES):
        try:
            response = session.get(url, params=params or {}, timeout=30)
            # WordPress returns 400 with rest_post_invalid_page_number when pagination ends
            if response.status_code == 400:
                try:
                    data = response.json()
                    if isinstance(data, dict) and data.get("code") in (
                        "rest_post_invalid_page_number",
                        "rest_term_invalid_page_number",
                    ):
                        return None
                except Exception:
                    pass
            response.raise_for_status()
            return response
        except Exception as exc:
            if attempt == _MAX_RETRIES - 1 or not _is_transient(exc):
                raise
            resp = getattr(exc, "response", None)
            delay = _retry_after(resp, attempt)
            logger.warning(
                "WordPress: %s — retrying in %.1fs (%d/%d).",
                exc,
                delay,
                attempt + 1,
                _MAX_RETRIES,
            )
            time.sleep(delay)


def _paginate_endpoint(
    session: Any,
    api_url: str,
    content_type: str,
    params: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Paginate through a WordPress REST endpoint using page & per_page."""
    endpoint = f"{api_url}/{content_type}"
    page = 1
    req_params = dict(params or {})
    req_params.setdefault("per_page", 100)

    while True:
        req_params["page"] = page
        response = _api_get(session, endpoint, req_params)
        if response is None:
            break

        data = response.json()
        if not data or not isinstance(data, list):
            break

        yield from data

        # Check total pages from WordPress response headers
        total_pages_hdr = response.headers.get("X-WP-TotalPages")
        if total_pages_hdr:
            try:
                if page >= int(total_pages_hdr):
                    break
            except (ValueError, TypeError):
                pass

        # Check Link header for next rel
        link_header = response.headers.get("Link", "")
        has_next_rel = 'rel="next"' in link_header
        if total_pages_hdr is None and not has_next_rel and len(data) < req_params["per_page"]:
            break

        page += 1


# ---------------------------------------------------------------------------
# HTML Parsing & Row formatting
# ---------------------------------------------------------------------------
def _clean_html(raw: str | None) -> str:
    """Convert raw WordPress HTML into clean plain text."""
    if not raw:
        return ""
    no_scripts = _SCRIPT_STYLE_RE.sub(" ", raw)
    no_tags = _TAG_RE.sub(" ", no_scripts)
    unescaped = html.unescape(no_tags)
    return _WS_RE.sub(" ", unescaped).strip()


def _item_to_row(item: dict[str, Any], content_type: str) -> dict[str, Any]:
    """Convert a WordPress API item into a cognee document row."""
    item_id = str(item.get("id"))
    composite_id = f"{content_type}:{item_id}"
    url = item.get("link") or ""

    if content_type == "comments":
        author = item.get("author_name") or "Anonymous"
        post_id = item.get("post")
        title = f"Comment by {author} on Post {post_id}"
        content = _clean_html(item.get("content", {}).get("rendered"))
    else:
        title_raw = item.get("title", {})
        title_text = (
            title_raw.get("rendered") if isinstance(title_raw, dict) else str(title_raw or "")
        )
        title = _clean_html(title_text)

        content_raw = item.get("content", {})
        content_text = (
            content_raw.get("rendered") if isinstance(content_raw, dict) else str(content_raw or "")
        )
        content = _clean_html(content_text)

        excerpt_raw = item.get("excerpt", {})
        excerpt_text = excerpt_raw.get("rendered") if isinstance(excerpt_raw, dict) else ""
        clean_excerpt = _clean_html(excerpt_text)
        if clean_excerpt and clean_excerpt not in content:
            content = f"{clean_excerpt}\n\n{content}".strip()

    return {
        "id": composite_id,
        "title": title,
        "content": content,
        "url": url,
        "_deleted": False,
    }


def _deleted_row(composite_id: str) -> dict[str, Any]:
    """Build a minimal row that instructs dlt to hard-delete an item by ID."""
    return {"id": composite_id, "_deleted": True}


# ---------------------------------------------------------------------------
# Sync logic: Incremental delta + Forget-on-Delete
# ---------------------------------------------------------------------------
def sync_wordpress(
    session: Any,
    api_url: str,
    state: dict[str, Any],
    content_types: list[str],
) -> Iterator[dict[str, Any]]:
    """Yield changed WordPress items since last run, plus hard-delete tombstones."""
    known_ids: set[str] = set(state.get("known_ids", []))
    last_modified: str = state.get("last_modified", "")
    newest_modified = last_modified
    current_ids: set[str] = set()
    changed_count = 0

    for content_type in content_types:
        # 1. Lightweight ID sweep: fetch item IDs to identify deletions
        id_params = {"_fields": "id"}
        try:
            for item in _paginate_endpoint(session, api_url, content_type, id_params):
                raw_id = item.get("id")
                if raw_id is not None:
                    current_ids.add(f"{content_type}:{raw_id}")
        except Exception as exc:
            logger.warning("WordPress: failed to fetch ID sweep for %s: %s", content_type, exc)
            raise

        # 2. Incremental fetch: fetch items modified after cursor
        fetch_params: dict[str, Any] = {"orderby": "modified", "order": "asc"}
        if last_modified:
            if content_type == "comments":
                fetch_params["after"] = last_modified
                fetch_params["orderby"] = "date"
            else:
                fetch_params["modified_after"] = last_modified

        for item in _paginate_endpoint(session, api_url, content_type, fetch_params):
            comp_id = f"{content_type}:{item.get('id')}"
            current_ids.add(comp_id)

            mod_time = (
                item.get("modified_gmt")
                or item.get("modified")
                or item.get("date_gmt")
                or item.get("date")
                or ""
            )
            if mod_time > newest_modified:
                newest_modified = mod_time

            yield _item_to_row(item, content_type)
            changed_count += 1

    # Safety check: if known_ids is non-empty but current_ids is empty,
    # it indicates a transient listing failure rather than a wipeout.
    if known_ids and not current_ids:
        logger.warning(
            "WordPress: ID sweep returned 0 items but %d were previously known; "
            "skipping deletion to avoid unintended wipeout.",
            len(known_ids),
        )
        state["last_modified"] = newest_modified
        return

    # 3. Detect and emit deleted items
    deleted_ids = known_ids - current_ids
    for del_id in sorted(deleted_ids):
        yield _deleted_row(del_id)

    # 4. Advance resource state
    state["known_ids"] = sorted(current_ids)
    state["last_modified"] = newest_modified
    logger.info(
        "WordPress: synced %d changed items, %d deletion(s).",
        changed_count,
        len(deleted_ids),
    )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def wordpress_source(
    base_url: str | None = None,
    username: str | None = None,
    app_password: str | None = None,
    content_types: list[str] | None = None,
    custom_post_types: list[str] | None = None,
    session: Any = None,
):
    """Create a dlt source yielding WordPress content as documents.

    Args:
        base_url: Base URL of the WordPress site (e.g. ``"https://example.com"``).
            Falls back to ``WORDPRESS_URL`` environment variable.
        username: WordPress username. Falls back to ``WORDPRESS_USERNAME``.
        app_password: WordPress Application Password. Falls back to
            ``WORDPRESS_APP_PASSWORD`` or ``WORDPRESS_API_KEY``.
        content_types: Content endpoints to ingest (default: ``["posts", "pages", "comments"]``).
        custom_post_types: Additional custom post type endpoints to ingest.
        session: Optional pre-configured requests session (primarily for test injection).

    Returns:
        A dlt resource configured with ``write_disposition="merge"``,
        ``primary_key="id"``, and document routing marker. Hand it to ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The WordPress connector requires dlt: pip install "dlt[sqlalchemy]".'
        ) from exc

    resolved_base_url = base_url or os.getenv("WORDPRESS_URL")
    if not resolved_base_url:
        raise ValueError(
            "base_url is required: pass base_url= or set WORDPRESS_URL environment variable."
        )

    resolved_username = username or os.getenv("WORDPRESS_USERNAME")
    resolved_password = (
        app_password or os.getenv("WORDPRESS_APP_PASSWORD") or os.getenv("WORDPRESS_API_KEY")
    )

    api_url = _normalize_api_url(resolved_base_url)

    selected_types = list(content_types or DEFAULT_CONTENT_TYPES)
    if custom_post_types:
        for cpt in custom_post_types:
            if cpt not in selected_types:
                selected_types.append(cpt)

    @dlt.resource(
        name=WORDPRESS_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def wordpress_content():
        client = session or _make_session(resolved_username, resolved_password)
        resource_state = dlt.current.resource_state()
        yield from sync_wordpress(client, api_url, resource_state, selected_types)

    resource = wordpress_content()
    # Opt into document mode routing so items flow through cognify entity extraction
    setattr(resource, DOCUMENT_SOURCE_ATTR, WORDPRESS_SOURCE_NAME)
    return resource
