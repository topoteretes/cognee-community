"""DLT source for Raindrop.io bookmarks (lastUpdate cursor + forget-on-delete).

Pulls Raindrop.io collections, bookmarks (raindrops), and their annotations
(highlights) into cognee incrementally — "ask my bookmarks".  The source is a
single dlt resource meant to be handed directly to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_raindrop import raindrop_source

    await cognee.remember(
        raindrop_source(token="..."),
        dataset_name="my_bookmarks",
        primary_key="id",
        write_disposition="merge",   # REQUIRED (see .. important:: below)
        max_rows_per_table=0,        # REQUIRED for a real account (see .. note:: below)
    )

.. important::
   ``write_disposition="merge"`` is **mandatory**.  The add pipeline defaults
   to ``"replace"`` (drop + reload the table each run); on the second,
   incremental sync that would wipe everything but the small delta.  Always
   pass ``"merge"``.

Design
------
* **Auth** — Raindrop.io test token (Settings → Integrations → For Developer)
  or an OAuth app token.  Pass ``token=`` or set ``RAINDROP_API_TOKEN``.  The
  connector only issues reads.
* **Primary key** — the Raindrop ``_id`` (as a string).  Combined with
  ``write_disposition="merge"`` this gives idempotent upserts across all three
  entity kinds (collections / bookmarks / annotations) in one staging table.
  Annotation row ids are prefixed ``hl-`` so they can never collide with a
  bookmark id.
* **Incremental cursor** — the ``lastUpdate`` timestamp.  Raindrops are listed
  newest-change-first (``sort=-lastUpdate``) and only rows updated after the
  cursor recorded in dlt's resource state are re-emitted.  The listing is
  still paged in full every run: the deletion sweep needs to see every live
  id, so unchanged rows are skipped client-side rather than skipped remotely.
* **Forget-on-delete** — Raindrop has no deletion feed, so — like the
  Confluence connector — each run does a full id sweep of the account and
  compares it against the ids seen on the previous run (kept in resource
  state).  Vanished bookmarks/collections — and the annotations of vanished
  bookmarks, plus annotations removed from a re-fetched one — are emitted with
  the ``_deleted`` hard-delete marker; dlt removes those rows on ``merge`` and
  cognee's existing ``orphan_cleanup`` purges them from the graph + vector +
  relational stores.  No parallel cleanup path.
* **Opt-in page content** — fetching the linked page's HTML and extracting its
  text multiplies ingest cost (one HTTP request per bookmark, every time the
  bookmark changes), so it is off by default and enabled with
  ``fetch_page_content=True``.  Without it, the bookmark document is title +
  Raindrop's own ``excerpt`` + tags.  Failures to fetch degrade gracefully to
  the excerpt-only document.
* **Document mode** — the resource declares ``cognee_document_source =
  "raindrop"`` (the same marker notion/google-drive use), so each row flows
  through the standard cognify entity-extraction pipeline as a normal text
  document instead of the deterministic dlt-row schema-context path.

.. note::
   cognee's ``ingest_dlt_source`` reads at most ``max_rows_per_table`` rows
   from the dlt destination (default 50).  For a real account pass
   ``max_rows_per_table=0`` (unlimited) so orphan-cleanup compares against the
   *whole* synced corpus rather than a truncated window.

Privacy
-------
This reads the content of your bookmarks (and, if opted in, the linked pages).
It is **opt-in**: nothing is fetched until you construct a source and call
``remember``.  Use a dedicated dataset so you can ``cognee.forget`` the whole
thing in one call.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Iterator
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("raindrop_connector")

# dlt resource / staging-table name for Raindrop objects (all three kinds live
# in one table, discriminated by the ``kind`` column).
RAINDROP_SOURCE_NAME = "raindrop"
RAINDROP_TABLE_NAME = "raindrop"

# Retry budget for rate-limited / transient Raindrop API responses.
_MAX_RETRIES = 5

# The all-raindrops collection id (includes unsorted). Raindrop caps pages at
# 50 items, so paging is driven by the response ``count``.
_ALL_COLLECTION_ID = "0"
_PAGE_SIZE = 50
_MAX_PAGES = 2000  # hard stop: 100k bookmarks
_MAX_PAGE_TEXT = 5000

_EXTRA_HINT = (
    'The Raindrop connector requires the "raindrop" extra: pip install "cognee[raindrop]" '
    "(provides dlt and httpx)."
)


def raindrop_source(
    token: str | None = None,
    fetch_page_content: bool = False,
    client: Any = None,
):
    """Create a dlt resource that yields Raindrop collections/bookmarks/annotations.

    Args:
        token: Raindrop.io API token. Falls back to ``RAINDROP_API_TOKEN``.
        fetch_page_content: Opt-in. When True, the linked page's HTML is
            fetched and its text appended to the bookmark document. This
            multiplies ingest cost (one HTTP request per changed bookmark) and
            re-fetches on every bookmark change — see the module docstring.
        client: Pre-built client (mainly a test-injection point); when omitted
            a :class:`RaindropClient` is built from the token above.

    Returns:
        A dlt resource suitable for ``cognee.remember(...)`` with
        ``write_disposition="merge"``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    if client is None:
        resolved_token = token or os.environ.get("RAINDROP_API_TOKEN")
        if not resolved_token:
            raise ValueError(
                "Raindrop.io API token required: pass token= or set RAINDROP_API_TOKEN."
            )
        client = RaindropClient(resolved_token)

    @dlt.resource(
        name=RAINDROP_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        # _deleted is a boolean hard-delete marker (matching gmail.py /
        # google_drive.py): rows where it is True are removed from the dlt
        # destination on merge, which propagates the deletion through
        # cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def raindrop():
        resource_state = dlt.current.resource_state()
        yield from _sync(client, resource_state, fetch_page_content=fetch_page_content)

    # Opt into the document ingestion path (row -> text document -> cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(raindrop, DOCUMENT_SOURCE_ATTR, RAINDROP_SOURCE_NAME)
    return raindrop


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class RaindropClient:
    """Minimal Raindrop.io REST v1 client over httpx, with retry/backoff.

    Exposes exactly the operations the connector needs: ``collections``,
    ``raindrops``, and the opt-in ``fetch_page``.  Any object with the same
    methods can be injected in its place (tests).
    """

    def __init__(
        self,
        token: str,
        base_url: str = "https://api.raindrop.io/rest/v1",
        timeout: float = 30.0,
    ):
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def collections(self) -> list[dict]:
        """All user collections: roots and nested children, deduplicated by id."""
        items: dict[int, dict] = {}
        for path in ("/collections", "/collections/childrens"):
            for page in range(0, _MAX_PAGES):
                payload = self._get(path, params={"per_page": _PAGE_SIZE, "page": page})
                batch = payload.get("items", [])
                items.update({item["_id"]: item for item in batch if "_id" in item})
                if len(batch) < _PAGE_SIZE:
                    break
        return list(items.values())

    def raindrops(self, collection_id: str = _ALL_COLLECTION_ID, page: int = 0) -> dict:
        """One page of raindrops (newest-change-first), 50 per page."""
        return self._get(
            f"/raindrops/{collection_id}",
            params={"per_page": _PAGE_SIZE, "page": page, "sort": "-lastUpdate"},
        )

    def fetch_page(self, url: str) -> str:
        """Best-effort text extraction from the linked page (opt-in only).

        No HTML parser dependency: title, meta description, and paragraph text
        are pulled with regexes and capped.  Any failure returns "" — the
        bookmark document then falls back to the API-provided excerpt.
        """
        try:
            import httpx

            resp = httpx.get(url, timeout=self._timeout, follow_redirects=True)
            resp.raise_for_status()
            return _extract_page_text(resp.text)
        except Exception as exc:
            logger.warning("Raindrop: page fetch failed for %s: %s", url, exc)
            return ""

    def _get(self, path: str, params: dict | None = None) -> dict:
        import httpx

        for attempt in range(_MAX_RETRIES):
            try:
                resp = httpx.get(
                    f"{self._base_url}{path}",
                    params=params,
                    headers={"Authorization": f"Bearer {self._token}"},
                    timeout=self._timeout,
                )
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:
                if attempt == _MAX_RETRIES - 1 or not _is_transient(exc):
                    raise
                delay = _retry_after(getattr(exc, "response", None), attempt)
                logger.warning(
                    "Raindrop: %s — retrying in %.1fs (%d/%d).",
                    exc,
                    delay,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(delay)


def _is_transient(exc: Exception) -> bool:
    """True for rate-limit / server / timeout / network errors worth retrying."""
    import httpx

    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return False


def _retry_after(response: Any, attempt: int) -> float:
    """Seconds to wait before retrying: the Retry-After header, else backoff."""
    header = None
    if response is not None:
        header = response.headers.get("retry-after") or response.headers.get("Retry-After")
    try:
        return float(header)
    except (TypeError, ValueError):
        return float(2**attempt)


# ---------------------------------------------------------------------------
# Page text extraction (regex, no parser dependency)
# ---------------------------------------------------------------------------

_SCRIPT_STYLE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)
_META_DESC = re.compile(
    r'<meta[^>]+name=["\'](?:description|og:description)["\'][^>]+content=["\']([^"\']*)["\']',
    re.I,
)
_PAGE_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")


def _extract_page_text(html: str) -> str:
    """Cheap HTML → text extraction: title, meta description, paragraphs."""
    if not html:
        return ""
    body = _SCRIPT_STYLE.sub(" ", html)
    parts: list[str] = []
    title = _PAGE_TITLE.search(body)
    if title and title.group(1).strip():
        parts.append(title.group(1).strip())
    desc = _META_DESC.search(html)  # meta tags live in <head>, above scripts
    if desc and desc.group(1).strip():
        parts.append(desc.group(1).strip())
    paragraphs = [_TAG.sub(" ", m).strip() for m in body.split("<p")[1:]] if "<p" in body else []
    text = "\n".join(p for p in parts + paragraphs if p)
    return text[:_MAX_PAGE_TEXT]


# ---------------------------------------------------------------------------
# Row builders (pure — unit-testable)
# ---------------------------------------------------------------------------


def _collection_row(collection: dict) -> dict:
    """Flatten a Raindrop collection into a document row."""
    cid = str(collection.get("_id") or "")
    return {
        "id": cid,
        "kind": "collection",
        "url": "",
        "title": collection.get("title") or "",
        "content": collection.get("description") or collection.get("title") or "",
        "_deleted": False,
    }


def _bookmark_row(raindrop: dict, page_text: str | None = None) -> dict:
    """Flatten a Raindrop bookmark into a document row.

    The document is title + API-provided excerpt + tags (+ the opt-in page
    text).  Volatile bookkeeping fields (collection refs, cover images,
    domains) are excluded so they don't churn the content-hash data_id.
    """
    rid = str(raindrop.get("_id") or "")
    title = raindrop.get("title") or raindrop.get("link") or ""
    tags = [t for t in (raindrop.get("tags") or []) if t]
    content = title
    if raindrop.get("excerpt"):
        content += f"\n\n{raindrop['excerpt'].strip()}"
    if tags:
        content += "\n\nTags: " + ", ".join(sorted(tags))
    if page_text:
        content += f"\n\n{page_text.strip()}"
    return {
        "id": rid,
        "kind": "bookmark",
        "url": raindrop.get("link") or "",
        "title": title,
        "content": content,
        "_deleted": False,
    }


def _annotation_rows(raindrop: dict) -> list[dict]:
    """Flatten a Raindrop's highlights (annotations) into document rows.

    Row ids are prefixed ``hl-`` so they can never collide with a bookmark id.
    """
    link = raindrop.get("link") or ""
    title = raindrop.get("title") or link
    rows = []
    for highlight in raindrop.get("highlights") or []:
        hid = highlight.get("_id")
        if hid is None:
            continue
        note = (highlight.get("note") or "").strip()
        text = (highlight.get("text") or "").strip()
        content = text if text else note
        if note and note != text:
            content = f"{text}\n\nNote: {note}" if text else note
        rows.append(
            {
                "id": f"hl-{hid}",
                "kind": "annotation",
                "url": link,
                "title": f"Highlight in {title}"[:200],
                "content": content,
                "_deleted": False,
            }
        )
    return rows


def _tombstone_row(row_id: str, kind: str = "") -> dict:
    """A hard-delete marker row for a vanished object."""
    return {"id": row_id, "kind": kind, "_deleted": True}


# ---------------------------------------------------------------------------
# Sync strategies (pure given client + state dict — unit-testable)
# ---------------------------------------------------------------------------


def _sync(client: Any, state: dict, fetch_page_content: bool = False) -> Iterator[dict]:
    """Run one sync pass against ``client``, recording cursors in ``state``.

    Only raindrops updated after the recorded ``last_update`` cursor are
    re-emitted (the listing is sorted newest-change-first, so paging stops at
    the first page that is entirely older).  The full id set seen each run is
    diffed against the previous run's to emit hard-delete markers for vanished
    objects — Raindrop has no deletion feed.
    """
    # --- Collections: sweep + upsert ---------------------------------------
    collections = client.collections()
    prev_collection_ids: set[str] = set(state.get("collection_ids") or [])
    curr_collection_ids: set[str] = set()
    for collection in collections:
        cid = str(collection.get("_id"))
        curr_collection_ids.add(cid)
        if cid in prev_collection_ids and not _is_newer(collection, state):
            continue
        yield _collection_row(collection)
    for cid in sorted(prev_collection_ids - curr_collection_ids):
        yield _tombstone_row(cid, "collection")
    state["collection_ids"] = sorted(curr_collection_ids)

    # --- Bookmarks + annotations: cursor'd listing + id sweep ---------------
    cursor = state.get("last_update")
    prev_bookmark_ids: set[str] = set(state.get("bookmark_ids") or [])
    prev_highlight_ids: dict[str, list[str]] = dict(state.get("highlight_ids") or {})

    curr_bookmark_ids: set[str] = set()
    curr_highlight_ids: dict[str, list[str]] = {}
    max_update = _parse_ts(cursor) if cursor else None

    page = 0
    while page < _MAX_PAGES:
        payload = client.raindrops(_ALL_COLLECTION_ID, page=page)
        items = payload.get("items", [])
        if not items:
            break

        for raindrop in items:
            rid = str(raindrop["_id"])
            # Alive either way — record it for the deletion sweep *before* the
            # cursor filter, or unchanged objects would be mistaken for
            # vanished ones.
            curr_bookmark_ids.add(rid)
            updated = _parse_ts(raindrop.get("lastUpdate"))
            if updated is not None and (max_update is None or updated > max_update):
                max_update = updated
            if cursor is not None and (updated is None or updated <= _parse_ts(cursor)):
                # Not changed since the last run — nothing to re-emit. The
                # sweep below still needs every id seen this run, so paging
                # walks the full listing (no early stop against the cursor:
                # that would mistake unvisited live bookmarks for deletions).
                continue

            link = raindrop.get("link") or ""
            page_text = None
            if fetch_page_content and _is_http_url(link):
                # Best-effort by contract: any client failure degrades to the
                # excerpt-only document instead of failing the sync.
                try:
                    page_text = client.fetch_page(link)
                except Exception as exc:
                    logger.warning("Raindrop: page fetch failed for %s: %s", link, exc)
            yield _bookmark_row(raindrop, page_text)

            rows = _annotation_rows(raindrop)
            curr_highlight_ids[rid] = [row["id"] for row in rows]
            for hid in sorted(set(prev_highlight_ids.get(rid, [])) - {row["id"] for row in rows}):
                yield _tombstone_row(hid, "annotation")
            yield from rows
            curr_bookmark_ids.add(rid)

        page += 1
        total = payload.get("count")
        if total is not None and page * _PAGE_SIZE >= total:
            break

    # Deletions: bookmarks (and their annotations) that vanished since the
    # previous run, plus annotations removed from bookmarks we just re-fetched.
    for rid in sorted(prev_bookmark_ids - curr_bookmark_ids):
        yield _tombstone_row(rid, "bookmark")
        for hid in prev_highlight_ids.get(rid, []):
            yield _tombstone_row(hid, "annotation")
        prev_highlight_ids.pop(rid, None)
    state["highlight_ids"] = curr_highlight_ids
    state["bookmark_ids"] = sorted(curr_bookmark_ids)
    state["last_update"] = max_update.isoformat() if max_update else cursor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_newer(collection: dict, state: dict) -> bool:
    """True when the collection was updated after the recorded cursor."""
    cursor = state.get("last_update")
    if not cursor:
        return True
    updated = _parse_ts(collection.get("lastUpdate"))
    return updated is None or updated > _parse_ts(cursor)


def _parse_ts(value: Any) -> datetime | None:
    """Parse a Raindrop ISO-8601 timestamp ('...Z' or '...+00:00') to UTC."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_http_url(url: str) -> bool:
    """True for http(s) URLs — skip javascript:/chrome: style bookmarklets."""
    return urlparse(url).scheme in ("http", "https")
