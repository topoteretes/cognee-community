"""Raindrop.io connector for cognee — a ``dlt`` source that turns your bookmarks into memory.

Pull Raindrop.io bookmarks, with their collection path, tags, notes, and
highlights (annotations), into cognee, incrementally and with forget-on-delete.
The resource produced here is handed directly to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_raindrop import raindrop_source

    await cognee.remember(
        raindrop_source(),           # RAINDROP_TOKEN from env, or pass token=...
        dataset_name="raindrop",
        write_disposition="merge",   # required, see the warning below
    )

Design
------
* **Auth** — a Raindrop.io test token or an OAuth access token. Both are sent as
  ``Authorization: Bearer <token>``. The connector only issues ``GET`` requests.
* **Document mode** — each bookmark is rendered to one markdown document (title,
  link, collection path, tags, description, note, and every highlight with its
  note). The resource declares ``DOCUMENT_SOURCE_ATTR``, so rows flow through
  normal cognify (LLM graph extraction) rather than the dlt-row schema path.
* **Incremental sync** — each run lists the bookmarks in scope (the list
  endpoint already carries highlights, so there is no per-bookmark request) and
  fingerprints each rendered document. Only bookmarks whose fingerprint differs
  from the previous run are emitted, so unchanged bookmarks are never re-embedded.
  The fingerprints live in dlt's per-resource state. Raindrop has no
  ``lastUpdate`` sort and its ``lastUpdate:`` search filter is day-granular, so
  the content fingerprint is the cursor. It also catches changes that do not
  bump a bookmark's ``lastUpdate``, such as renaming its parent collection.
* **Forget-on-delete** — Raindrop has no deletion feed, and trashed bookmarks
  drop out of the listing (Trash is collection ``-99``). Ids seen last run but
  missing now are emitted with the ``_deleted`` hard-delete marker; dlt removes
  those rows on ``merge`` and cognee's existing ``orphan_cleanup`` purges them
  from the graph, vector, and relational stores.
* **Linked page content** — opt-in via ``fetch_page_content=True``, because it
  multiplies ingest cost. Pages are fetched without the Raindrop token, with a
  timeout and a size cap, and only when the bookmark itself is new or changed.

.. warning::
   Pass ``write_disposition="merge"`` to ``remember``/``add``. cognee defaults
   to ``"replace"``, which overrides the resource's own setting. Because each
   run only emits new/changed bookmarks, ``"replace"`` would leave staging with
   just that delta and orphan-cleanup would forget every unchanged bookmark.
"""

from __future__ import annotations

import hashlib
import html
import os
import re
import time
from collections.abc import Callable, Iterator
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("raindrop_connector")

# dlt resource / staging-table name for Raindrop bookmarks.
RAINDROP_TABLE_NAME = "raindrop_bookmarks"
RAINDROP_SOURCE_NAME = "raindrop"

API_BASE_URL = "https://api.raindrop.io/rest/v1"

# Raindrop collection id 0 lists every bookmark except those in Trash.
ALL_COLLECTIONS = 0
UNSORTED_COLLECTION = -1

# The raindrops list endpoint caps perpage at 50.
_PER_PAGE = 50

# Retry budget for rate-limited / transient Raindrop API responses. Raindrop
# allows 120 requests per minute per user and answers 429 past that.
_MAX_RETRIES = 5
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
# Never sleep longer than this on a single retry, whatever the headers say.
_MAX_RETRY_DELAY = 60.0

# Linked page fetching (opt-in) limits.
_PAGE_TIMEOUT_SECONDS = 10.0
_PAGE_MAX_BYTES = 2 * 1024 * 1024
_DEFAULT_MAX_PAGE_CHARS = 10_000

_DROP_BLOCKS_RE = re.compile(r"<(script|style|noscript)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

_EXTRA_HINT = (
    'The Raindrop connector requires "dlt" and "httpx": '
    "pip install cognee-community-connector-raindrop"
)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def _make_client(token: str) -> Any:
    """Build an ``httpx`` client authenticated against the Raindrop REST API."""
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - depends on installed extras
        raise ImportError(_EXTRA_HINT) from exc

    return httpx.Client(
        base_url=API_BASE_URL,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=30.0,
    )


def _api_get(client: Any, path: str, params: dict | None = None) -> dict:
    """GET a Raindrop API path and return its JSON body, retrying transient errors.

    Rate limits (429), server errors (5xx), and network errors are retried with
    the delay the server asks for, else exponential backoff. Permanent errors
    (bad token, unknown collection) and exhausted retries raise, which aborts
    the run and leaves dlt state, and therefore memory, untouched.
    """
    import httpx

    for attempt in range(_MAX_RETRIES):
        last_attempt = attempt == _MAX_RETRIES - 1
        try:
            response = client.get(path, params=params)
        except httpx.TransportError as exc:
            if last_attempt:
                raise
            delay = float(2**attempt)
            logger.warning(
                "Raindrop: %s — retrying in %.1fs (%d/%d).", exc, delay, attempt + 1, _MAX_RETRIES
            )
            time.sleep(delay)
            continue

        if response.status_code in _RETRYABLE_STATUS and not last_attempt:
            delay = _retry_delay(response.headers, attempt)
            logger.warning(
                "Raindrop: HTTP %d on %s — retrying in %.1fs (%d/%d).",
                response.status_code,
                path,
                delay,
                attempt + 1,
                _MAX_RETRIES,
            )
            time.sleep(delay)
            continue

        response.raise_for_status()
        return response.json()

    raise AssertionError("unreachable")  # pragma: no cover


def _retry_delay(headers: Any, attempt: int) -> float:
    """Seconds to wait before retrying.

    Prefers ``Retry-After`` (seconds), then ``X-RateLimit-Reset`` (UTC epoch
    seconds when the rate-limit window resets), then exponential backoff.
    """
    headers = headers or {}
    retry_after = headers.get("Retry-After")
    if retry_after is not None:
        try:
            return min(max(float(retry_after), 0.0), _MAX_RETRY_DELAY)
        except ValueError:
            pass

    reset = headers.get("X-RateLimit-Reset")
    if reset is not None:
        try:
            return min(max(float(reset) - time.time(), 0.0) + 1.0, _MAX_RETRY_DELAY)
        except ValueError:
            pass

    return float(2**attempt)


# ---------------------------------------------------------------------------
# Raindrop API reads
# ---------------------------------------------------------------------------
def _collection_paths(client: Any) -> dict[int, str]:
    """Map every collection id to its breadcrumb path, e.g. ``Research/AI``.

    Root collections come from ``/collections`` and nested ones from
    ``/collections/childrens`` (the API's spelling); a child points at its
    parent through ``parent.$id``.
    """
    collections: dict[int, dict] = {}
    for path in ("/collections", "/collections/childrens"):
        for item in _api_get(client, path).get("items") or []:
            if "_id" in item:
                collections[item["_id"]] = item

    paths: dict[int, str] = {UNSORTED_COLLECTION: "Unsorted"}

    def resolve(collection_id: int, seen: frozenset = frozenset()) -> str:
        if collection_id in paths:
            return paths[collection_id]
        item = collections.get(collection_id)
        if item is None or collection_id in seen:
            return ""
        title = item.get("title") or str(collection_id)
        parent_id = (item.get("parent") or {}).get("$id")
        parent = resolve(parent_id, seen | {collection_id}) if parent_id is not None else ""
        paths[collection_id] = f"{parent}/{title}" if parent else title
        return paths[collection_id]

    for collection_id in collections:
        resolve(collection_id)
    return paths


def _iter_raindrops(
    client: Any, collection_ids: list[int] | None, include_nested: bool
) -> Iterator[dict]:
    """Yield every bookmark in scope, each at most once.

    Scoped collections may overlap (a parent with ``nested`` plus one of its
    children), so bookmarks are de-duplicated by id.
    """
    seen: set[int] = set()
    for collection_id in collection_ids or [ALL_COLLECTIONS]:
        params: dict[str, Any] = {"perpage": _PER_PAGE}
        if collection_id != ALL_COLLECTIONS and include_nested:
            params["nested"] = "true"

        page = 0
        while True:
            data = _api_get(client, f"/raindrops/{collection_id}", {**params, "page": page})
            items = data.get("items") or []
            for item in items:
                raindrop_id = item.get("_id")
                # A removed bookmark is treated as gone, so it is tombstoned
                # rather than re-ingested.
                if raindrop_id is None or raindrop_id in seen or item.get("removed"):
                    continue
                seen.add(raindrop_id)
                yield item
            # A short (or empty) page is the last one.
            if len(items) < _PER_PAGE:
                break
            page += 1


# ---------------------------------------------------------------------------
# Bookmark → document
# ---------------------------------------------------------------------------
def _collection_id(item: dict) -> int | None:
    """Return the id of the collection a bookmark lives in.

    The live API returns it as ``collectionId`` and leaves ``collection.$id``
    unset, although the docs only describe ``collection.$id``. Read both.
    """
    collection_id = item.get("collectionId")
    if collection_id is None:
        collection_id = (item.get("collection") or {}).get("$id")
    return collection_id


def _render_bookmark(item: dict, collection_path: str, include_highlights: bool) -> str:
    """Render one bookmark (and its highlights) to a markdown document.

    Only user-meaningful, stable fields are included. Volatile metadata such as
    ``lastUpdate`` is left out so it cannot churn the content fingerprint or
    cognee's content-hash ``data_id``.
    """
    title = (item.get("title") or "").strip() or (item.get("link") or "Untitled bookmark")
    lines = [f"# {title}", ""]

    if item.get("link"):
        lines.append(f"URL: {item['link']}")
    if collection_path:
        lines.append(f"Collection: {collection_path}")
    if item.get("type"):
        lines.append(f"Type: {item['type']}")
    tags = [tag for tag in item.get("tags") or [] if tag]
    if tags:
        lines.append(f"Tags: {', '.join(tags)}")
    if item.get("created"):
        lines.append(f"Saved: {item['created']}")

    excerpt = (item.get("excerpt") or "").strip()
    if excerpt:
        lines += ["", "## Description", "", excerpt]

    note = (item.get("note") or "").strip()
    if note:
        lines += ["", "## Note", "", note]

    if include_highlights:
        highlights = [h for h in item.get("highlights") or [] if (h.get("text") or "").strip()]
        if highlights:
            lines += ["", "## Highlights"]
            for highlight in highlights:
                lines += ["", f"> {highlight['text'].strip()}"]
                highlight_note = (highlight.get("note") or "").strip()
                if highlight_note:
                    lines.append(f"Note: {highlight_note}")

    return "\n".join(lines).strip()


def _fingerprint(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _bookmark_row(item: dict, content: str) -> dict[str, Any]:
    """Document-mode row contract: ``{id, title, content, url}`` plus the delete marker."""
    return {
        "id": str(item["_id"]),
        "title": (item.get("title") or "").strip() or (item.get("link") or ""),
        "content": content,
        "url": item.get("link") or "",
        # Hard-delete marker (always False for live bookmarks). Vanished
        # bookmarks are emitted separately with _deleted=True.
        "_deleted": False,
    }


def _deleted_row(raindrop_id: str) -> dict[str, Any]:
    """Build a minimal row that instructs dlt to hard-delete a bookmark by id."""
    return {"id": str(raindrop_id), "_deleted": True}


# ---------------------------------------------------------------------------
# Linked page content (opt-in)
# ---------------------------------------------------------------------------
def _html_to_text(raw: str) -> str:
    """Strip HTML down to plain text. Dependency-free on purpose."""
    without_blocks = _DROP_BLOCKS_RE.sub(" ", raw)
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", without_blocks))).strip()


def _fetch_page_text(page_client: Any, url: str, max_chars: int) -> str:
    """Fetch a bookmark's linked page and return its text, or ``""`` to skip it.

    The page client never carries the Raindrop token. The body is read up to a
    byte cap, only HTML and plain text are kept, and any failure is logged and
    skipped: a slow or broken site must not abort the whole sync.
    """
    if not url.startswith(("http://", "https://")):
        return ""
    try:
        with page_client.stream("GET", url) as response:
            if response.status_code >= 400:
                logger.warning("Raindrop: skipping page %s (HTTP %d).", url, response.status_code)
                return ""
            content_type = response.headers.get("content-type", "").lower()
            if content_type and not content_type.startswith(("text/html", "text/plain")):
                return ""
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) >= _PAGE_MAX_BYTES:
                    break
            text = bytes(body[:_PAGE_MAX_BYTES]).decode(response.encoding or "utf-8", "replace")
    except Exception as exc:
        logger.warning("Raindrop: skipping page %s: %s", url, exc)
        return ""

    if "html" in content_type or not content_type:
        text = _html_to_text(text)
    return text[:max_chars].strip()


def _make_page_client() -> Any:
    import httpx

    return httpx.Client(
        timeout=_PAGE_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"User-Agent": "cognee-raindrop-connector"},
    )


# ---------------------------------------------------------------------------
# Sync (pure given a client + state dict — unit-testable)
# ---------------------------------------------------------------------------
def sync_bookmarks(
    client: Any,
    state: dict,
    *,
    collection_ids: list[int] | None = None,
    include_nested: bool = True,
    include_highlights: bool = True,
    fetch_page: Callable[[str], str] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield new/changed bookmarks since the last run, plus hard-delete markers.

    ``state["fingerprints"]`` maps bookmark id to the fingerprint of its
    rendered document from the previous run. It is replaced with the current
    sweep at the end, so a second run with nothing changed yields nothing.

    Args:
        client: Authenticated Raindrop API client.
        state: dlt resource state (a plain dict in tests).
        collection_ids: Collections to sync. ``None`` syncs everything but Trash.
        include_nested: Also sync bookmarks in nested collections of each scoped
            collection.
        include_highlights: Fold each bookmark's highlights into its document.
        fetch_page: Optional ``url -> text`` callable; when given, the linked
            page text is appended to new or changed bookmarks.
    """
    known: dict[str, str] = dict(state.get("fingerprints") or {})
    current: dict[str, str] = {}
    paths = _collection_paths(client)
    changed = 0

    for item in _iter_raindrops(client, collection_ids, include_nested):
        raindrop_id = str(item["_id"])
        content = _render_bookmark(item, paths.get(_collection_id(item), ""), include_highlights)

        fingerprint = _fingerprint(content)
        current[raindrop_id] = fingerprint
        if known.get(raindrop_id) == fingerprint:
            continue

        if fetch_page is not None and item.get("link"):
            page_text = fetch_page(item["link"])
            if page_text:
                content = f"{content}\n\n## Page content\n\n{page_text}"

        changed += 1
        yield _bookmark_row(item, content)

    # Deletion detection relies on the sweep seeing every live bookmark. An
    # empty sweep while bookmarks were known almost always means a failed or
    # mis-scoped listing rather than a wiped library. Treating it as "all
    # deleted" would purge the whole dataset and forget every fingerprint, so
    # skip deletion and keep the previous state instead.
    if known and not current:
        logger.warning(
            "Raindrop: sweep returned 0 bookmarks but %d were known; skipping "
            "deletion this run to avoid a mass forget-on-delete on a bad sweep.",
            len(known),
        )
        return

    deleted = sorted(known.keys() - current.keys())
    for raindrop_id in deleted:
        yield _deleted_row(raindrop_id)

    state["fingerprints"] = current
    logger.info("Raindrop: %d new/changed bookmark(s), %d deletion(s).", changed, len(deleted))


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def raindrop_source(
    token: str | None = None,
    *,
    collection_ids: list[int] | None = None,
    include_nested: bool = True,
    include_highlights: bool = True,
    fetch_page_content: bool = False,
    max_page_chars: int = _DEFAULT_MAX_PAGE_CHARS,
    client: Any = None,
    page_client: Any = None,
):
    """Return a ``dlt`` resource that yields Raindrop.io bookmarks for ``remember``.

    Args:
        token: Raindrop.io test token or OAuth access token. Falls back to the
            ``RAINDROP_TOKEN`` environment variable.
        collection_ids: Restrict the sync to these collection ids (``-1`` is
            Unsorted). ``None`` syncs every bookmark outside Trash.
        include_nested: With ``collection_ids``, also sync their nested
            collections (default True).
        include_highlights: Fold highlights and their notes into each bookmark
            document (default True).
        fetch_page_content: Also fetch and ingest each bookmark's linked page
            text. Off by default because it multiplies ingest cost.
        max_page_chars: Keep at most this many characters of page text per
            bookmark when ``fetch_page_content`` is on.
        client: Pre-built Raindrop API client. Mainly an injection point for
            tests; when omitted one is built from ``token``.
        page_client: Pre-built client for linked page fetches (test injection
            point). It must not carry the Raindrop token.

    Returns:
        A ``dlt`` resource (``raindrop_bookmarks``) configured with
        ``primary_key="id"``, ``write_disposition="merge"`` and an ``_deleted``
        hard-delete column. Hand it to ``cognee.remember(...)`` with
        ``write_disposition="merge"``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    resolved_token = token or os.environ.get("RAINDROP_TOKEN")
    if client is None and not resolved_token:
        raise ValueError(
            "Raindrop.io token required: pass token= or set RAINDROP_TOKEN "
            "(a test token from https://app.raindrop.io/settings/integrations works)."
        )
    if max_page_chars <= 0:
        raise ValueError("max_page_chars must be a positive number of characters.")

    @dlt.resource(
        name=RAINDROP_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        # _deleted is a boolean hard-delete marker: rows where it is True are
        # removed from the dlt destination on merge, which propagates the
        # deletion through cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def raindrop_bookmarks():
        api_client = client or _make_client(resolved_token)
        fetch_page = None
        if fetch_page_content:
            pages = page_client or _make_page_client()

            def fetch_page(url: str) -> str:
                return _fetch_page_text(pages, url, max_page_chars)

        yield from sync_bookmarks(
            api_client,
            dlt.current.resource_state(),
            collection_ids=collection_ids,
            include_nested=include_nested,
            include_highlights=include_highlights,
            fetch_page=fetch_page,
        )

    resource = raindrop_bookmarks()
    # Opt into the document ingestion path: each bookmark row becomes a text
    # document that flows through normal cognify. resolve_dlt_sources reads this
    # marker; it never imports this connector.
    setattr(resource, DOCUMENT_SOURCE_ATTR, RAINDROP_SOURCE_NAME)
    return resource
