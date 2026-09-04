"""DLT source for Contentful (updatedAt cursor + forget-on-delete).

Pulls Contentful content types (the content model), entries, and assets into
cognee incrementally — "ask my CMS".  The source is a single dlt resource
meant to be handed directly to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_contentful import contentful_source

    await cognee.remember(
        contentful_source(space_id="...", token="..."),
        dataset_name="my_cms",
        primary_key="id",
        write_disposition="merge",   # REQUIRED (see .. important:: below)
        max_rows_per_table=0,        # REQUIRED for a real space (see .. note:: below)
    )

.. important::
   ``write_disposition="merge"`` is **mandatory**.  The add pipeline defaults
   to ``"replace"`` (drop + reload the table each run); on the second,
   incremental sync that would wipe everything but the small delta.  Always
   pass ``"merge"``.

Design
------
* **Auth** — a Contentful *Content Delivery API* access token.  Pass
  ``token=`` or set ``CONTENTFUL_TOKEN`` (plus ``CONTENTFUL_SPACE_ID``, or
  ``space_id=``).  The connector only issues reads.
* **Primary key** — the Contentful ``sys.id``.  Combined with
  ``write_disposition="merge"`` this gives idempotent upserts across all
  three entity kinds (content types / entries / assets) in one staging table.
* **Incremental cursor** — ``sys.updatedAt``.  Listings are fetched
  ``order=-sys.updatedAt`` and only objects updated after the cursor recorded
  in dlt's resource state are re-emitted.  The listing is still paged in full
  every run: the deletion sweep needs to see every live id.
* **Forget-on-delete** — Contentful's Delivery API has no deletion feed, so
  each run diffs the full id set of every kind against the previous run's
  (kept in resource state).  Vanished objects are emitted with the ``_deleted``
  hard-delete marker; dlt removes those rows on ``merge`` and cognee's
  existing ``orphan_cleanup`` purges them from the graph + vector + relational
  stores.  No parallel cleanup path.
* **Content model** — content types are ingested too (as their own document
  rows describing every field), so entries have their schema context next to
  them in memory.
* **Document mode** — the resource declares ``cognee_document_source =
  "contentful"`` (the same marker notion/google-drive use), so each row flows
  through the standard cognify entity-extraction pipeline as a normal text
  document instead of the deterministic dlt-row schema-context path.  Entry
  bodies are flattened from their ``fields`` maps (strings, lists, rich text
  nodes, and link placeholders); links are *not* dereferenced, so a single
  entry edit never pulls the whole linked graph.

.. note::
   cognee's ``ingest_dlt_source`` reads at most ``max_rows_per_table`` rows
   from the dlt destination (default 50).  For a real space pass
   ``max_rows_per_table=0`` (unlimited) so orphan-cleanup compares against the
   *whole* synced corpus rather than a truncated window.

Privacy
-------
This reads the content of your Contentful space.  It is **opt-in**: nothing is
fetched until you construct a source and call ``remember``.  Use a dedicated
dataset so you can ``cognee.forget`` the whole thing in one call.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("contentful_connector")

# dlt resource / staging-table name for Contentful objects (all three kinds
# live in one table, discriminated by the ``kind`` column).
CONTENTFUL_SOURCE_NAME = "contentful"
CONTENTFUL_TABLE_NAME = "contentful"

# Retry budget for rate-limited / transient Contentful API responses.
_MAX_RETRIES = 5

# Contentful caps a page at 1000 items; stay well inside it.
_PAGE_SIZE = 200
_MAX_PAGES = 500  # hard stop: 100k objects of one kind

_EXTRA_HINT = (
    'The Contentful connector requires the "contentful" extra: pip install "cognee[contentful]" '
    "(provides dlt and httpx)."
)


def contentful_source(
    space_id: str | None = None,
    token: str | None = None,
    environment: str = "master",
    client: Any = None,
):
    """Create a dlt resource that yields Contentful content types/entries/assets.

    Args:
        space_id: The Contentful space id. Falls back to ``CONTENTFUL_SPACE_ID``.
        token: A Content Delivery API access token. Falls back to
            ``CONTENTFUL_TOKEN``.
        environment: The Contentful environment id (default ``"master"``).
        client: Pre-built client (mainly a test-injection point); when omitted
            a :class:`ContentfulClient` is built from the parameters above.

    Returns:
        A dlt resource suitable for ``cognee.remember(...)`` with
        ``write_disposition="merge"``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    if client is None:
        resolved_space = space_id or os.environ.get("CONTENTFUL_SPACE_ID")
        resolved_token = token or os.environ.get("CONTENTFUL_TOKEN")
        if not resolved_space or not resolved_token:
            raise ValueError(
                "Contentful space_id and token required: pass them or set "
                "CONTENTFUL_SPACE_ID / CONTENTFUL_TOKEN."
            )
        client = ContentfulClient(resolved_space, resolved_token, environment)

    @dlt.resource(
        name=CONTENTFUL_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        # _deleted is a boolean hard-delete marker (matching gmail.py /
        # google_drive.py): rows where it is True are removed from the dlt
        # destination on merge, which propagates the deletion through
        # cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def contentful():
        resource_state = dlt.current.resource_state()
        yield from _sync(client, resource_state)

    # Opt into the document ingestion path (row -> text document -> cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(contentful, DOCUMENT_SOURCE_ATTR, CONTENTFUL_SOURCE_NAME)
    return contentful


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class ContentfulClient:
    """Minimal Contentful Delivery API client over httpx, with retry/backoff.

    Exposes exactly the operations the connector needs: ``content_types``,
    ``entries``, and ``assets``.  Any object with the same methods can be
    injected in its place (tests).
    """

    def __init__(
        self,
        space_id: str,
        token: str,
        environment: str = "master",
        base_url: str = "https://cdn.contentful.com",
        timeout: float = 30.0,
    ):
        self._base = f"{base_url.rstrip('/')}/spaces/{space_id}/environments/{environment}"
        self._token = token
        self._timeout = timeout

    def content_types(self, skip: int = 0) -> dict:
        """One page of the content model, newest-change-first."""
        return self._get(
            "/content_types", params={"limit": _PAGE_SIZE, "skip": skip, "order": "-sys.updatedAt"}
        )

    def entries(self, skip: int = 0) -> dict:
        """One page of entries, newest-change-first."""
        return self._get(
            "/entries", params={"limit": _PAGE_SIZE, "skip": skip, "order": "-sys.updatedAt"}
        )

    def assets(self, skip: int = 0) -> dict:
        """One page of assets, newest-change-first."""
        return self._get(
            "/assets", params={"limit": _PAGE_SIZE, "skip": skip, "order": "-sys.updatedAt"}
        )

    def _get(self, path: str, params: dict) -> dict:
        import httpx

        for attempt in range(_MAX_RETRIES):
            try:
                resp = httpx.get(
                    f"{self._base}{path}",
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
                    "Contentful: %s — retrying in %.1fs (%d/%d).",
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
# Field flattening (pure — unit-testable)
# ---------------------------------------------------------------------------


def _rich_text_nodes(node: Any) -> Iterator[str]:
    """Yield plain text blocks from a Contentful rich-text JSON tree.

    Text inside one block element (paragraph, heading, …) is joined without
    separators; separate blocks come back as separate strings.
    """
    if isinstance(node, list):
        for child in node:
            yield from _rich_text_nodes(child)
    elif isinstance(node, dict):
        if node.get("nodeType") == "text":
            value = node.get("value")
            if value:
                yield value
        else:
            text = "".join(_rich_text_nodes(node.get("content")))
            if text:
                yield text


def _field_text(value: Any) -> str:
    """Flatten one Contentful field value to plain text.

    Strings, numbers, booleans, and lists of them pass through; rich-text
    objects render their text nodes; links render as ``[linkType: id]``
    placeholders (never dereferenced); anything else is skipped.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (str, int, float)):
        return str(value)
    if isinstance(value, list):
        return "\n".join(t for t in (_field_text(item) for item in value) if t)
    if isinstance(value, dict):
        if "nodeType" in value:  # rich text
            return "\n".join(_rich_text_nodes(value)).strip()
        sysinfo = value.get("sys")
        if isinstance(sysinfo, dict) and sysinfo.get("linkType") and sysinfo.get("id"):
            return f"[{sysinfo['linkType']}: {sysinfo['id']}]"
    return ""


def _flatten_fields(fields: dict) -> str:
    """Flatten an entry's ``fields`` map to one text block."""
    parts = []
    for name, value in (fields or {}).items():
        text = _field_text(value)
        if text:
            parts.append(f"{name}: {text}" if "\n" not in text else f"{name}:\n{text}")
    return "\n\n".join(parts)


def _entry_title(entry: dict, content_types: dict[str, dict]) -> str:
    """Best-effort entry title: the content type's display field, else its id."""
    fields = entry.get("fields") or {}
    ctype_id = (entry.get("sys") or {}).get("contentType", {}).get("sys", {}).get("id") or ""
    display = (content_types.get(ctype_id) or {}).get("displayField")
    if display and fields.get(display):
        text = _field_text(fields[display])
        if text:
            return text
    for value in fields.values():
        text = _field_text(value)
        if text:
            return text.split("\n")[0]
    return ctype_id or str((entry.get("sys") or {}).get("id") or "")


# ---------------------------------------------------------------------------
# Row builders (pure — unit-testable)
# ---------------------------------------------------------------------------


def _content_type_row(content_type: dict) -> dict:
    """Flatten a content type (the model) into a document row."""
    sysinfo = content_type.get("sys") or {}
    fields_desc = "\n".join(
        f"- {f.get('id')} ({f.get('type')})"
        for f in content_type.get("fields") or []
        if f.get("id")
    )
    name = content_type.get("name") or sysinfo.get("id") or ""
    content = name
    if content_type.get("displayField"):
        content += f"\n\nDisplay field: {content_type['displayField']}"
    if fields_desc:
        content += f"\n\nFields:\n{fields_desc}"
    return {
        "id": str(sysinfo.get("id") or ""),
        "kind": "content_type",
        "url": "",
        "title": name,
        "content": content,
        "_deleted": False,
    }


def _entry_row(entry: dict, content_types: dict[str, dict]) -> dict:
    """Flatten a Contentful entry into a document row."""
    sysinfo = entry.get("sys") or {}
    return {
        "id": str(sysinfo.get("id") or ""),
        "kind": "entry",
        "url": "",
        "title": _entry_title(entry, content_types),
        "content": _flatten_fields(entry.get("fields")),
        "_deleted": False,
    }


def _asset_row(asset: dict) -> dict:
    """Flatten a Contentful asset into a document row."""
    sysinfo = asset.get("sys") or {}
    fields = asset.get("fields") or {}
    fileinfo = fields.get("file") or {}
    if isinstance(fileinfo, dict):
        url = fileinfo.get("url") or ""
        if url.startswith("//"):
            url = f"https:{url}"
        details = f"{fileinfo.get('contentType') or ''} {fileinfo.get('fileName') or ''}".strip()
    else:
        url, details = "", ""
    title = fields.get("title") or fields.get("fileName") or sysinfo.get("id") or ""
    content = title
    if fields.get("description"):
        content += f"\n\n{fields['description']}"
    if details:
        content += f"\n\n{details}"
    return {
        "id": str(sysinfo.get("id") or ""),
        "kind": "asset",
        "url": url,
        "title": title,
        "content": content,
        "_deleted": False,
    }


def _tombstone_row(row_id: str, kind: str = "") -> dict:
    """A hard-delete marker row for a vanished object."""
    return {"id": row_id, "kind": kind, "_deleted": True}


# ---------------------------------------------------------------------------
# Sync strategies (pure given client + state dict — unit-testable)
# ---------------------------------------------------------------------------


def _sync(client: Any, state: dict) -> Iterator[dict]:
    """Run one sync pass against ``client``, recording cursors in ``state``.

    Only objects updated after the recorded ``last_update`` cursor are
    re-emitted (each listing is sorted newest-change-first).  Every listing is
    paged in full so the per-kind id sweep can emit hard-delete markers for
    objects that vanished upstream — Contentful's Delivery API has no deletion
    feed.
    """
    started = state.get("last_update")
    cursor_dt = _parse_ts(started)
    max_update = cursor_dt

    prev_ids: dict[str, set[str]] = {k: set(v) for k, v in (state.get("ids") or {}).items()}
    curr_ids: dict[str, set[str]] = {"content_type": set(), "entry": set(), "asset": set()}

    content_types = _page_all(client.content_types)
    for content_type in content_types:
        cid = str((content_type.get("sys") or {}).get("id") or "")
        curr_ids["content_type"].add(cid)
        max_update = _bump(max_update, content_type)
        if _changed(content_type, cursor_dt):
            yield _content_type_row(content_type)
    types_by_id = {str((ct.get("sys") or {}).get("id") or ""): ct for ct in content_types}

    for entry in _page_all(client.entries):
        eid = str((entry.get("sys") or {}).get("id") or "")
        curr_ids["entry"].add(eid)
        max_update = _bump(max_update, entry)
        if _changed(entry, cursor_dt):
            yield _entry_row(entry, types_by_id)

    for asset in _page_all(client.assets):
        aid = str((asset.get("sys") or {}).get("id") or "")
        curr_ids["asset"].add(aid)
        max_update = _bump(max_update, asset)
        if _changed(asset, cursor_dt):
            yield _asset_row(asset)

    for kind, prev in prev_ids.items():
        for oid in sorted(prev - curr_ids.get(kind, set())):
            yield _tombstone_row(oid, kind)

    state["ids"] = {k: sorted(v) for k, v in curr_ids.items()}
    state["last_update"] = max_update.isoformat() if max_update else started


def _page_all(fetch_page) -> list[dict]:
    """Collect one object kind across all pages of its listing."""
    items: list[dict] = []
    skip = 0
    for _ in range(_MAX_PAGES):
        payload = fetch_page(skip=skip)
        batch = payload.get("items", [])
        items.extend(batch)
        total = payload.get("total")
        skip += len(batch)
        if not batch or (total is not None and skip >= total):
            break
    return items


def _changed(obj: dict, cursor_dt: datetime | None) -> bool:
    """True when the object's updatedAt is after the cursor (or there is none)."""
    if cursor_dt is None:
        return True
    updated = _parse_ts((obj.get("sys") or {}).get("updatedAt"))
    return updated is None or updated > cursor_dt


def _bump(max_update: datetime | None, obj: dict) -> datetime | None:
    """Advance the running cursor with the object's updatedAt, if newer."""
    updated = _parse_ts((obj.get("sys") or {}).get("updatedAt"))
    if updated is None:
        return max_update
    if max_update is None or updated > max_update:
        return updated
    return max_update


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_ts(value: Any) -> datetime | None:
    """Parse a Contentful ISO-8601 timestamp ('...Z' or '...+00:00') to UTC."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
