"""DLT source for Notion pages (full-snapshot sync + forget-on-delete).

Fetches Notion pages and renders their block children to markdown, then yields
them as a dlt resource for cognee's ingestion pipeline.

Unlike the relational dlt path (SQL/CSV), Notion pages are ingested as *normal
documents*: the source declares `cognee_document_source = "notion"`, so
`resolve_dlt_sources` tags each row `external_metadata["source"] = "notion"`
(not `"dlt"`). `is_dlt_sourced` therefore returns False and each page flows
through the standard cognify entity-extraction pipeline - the right treatment
for prose - instead of the deterministic dlt-row schema-context path.

The source is a full snapshot: `write_disposition="replace"` rewrites staging
with exactly the pages currently visible to the integration each run. Deletions
propagate for free - an archived, trashed, or unshared page simply drops out of
Notion's listings, so it is absent from the snapshot and cognee's existing
`orphan_cleanup` removes it from the graph and vector stores. Unchanged pages
keep a stable content-hash `data_id`, so they are not re-ingested or
re-cognified. (Notion has no delete feed, and search/database queries omit
trashed pages rather than returning them flagged, so a merge + `hard_delete`
approach cannot see deletions - hence the Slack-style full-snapshot model.)

Incremental block-fetch (watermark)
-------------------------------------
Every sync still *lists* all visible pages (cheap paginated search/database
calls) so the full-snapshot contract is preserved and orphan_cleanup can detect
deletions. However, `_render_blocks` (the expensive recursive
`blocks.children.list` path) is **skipped** for pages whose
`last_edited_time` has not advanced since the previous run.

After a successful run the watermark store (a JSON file keyed by page id) is
written atomically.  A render failure propagates without updating the watermark,
leaving the previous good snapshot - and the previous watermark - intact for the
next attempt.

The watermark file location defaults to `~/.cognee/notion_watermark.json` and
can be overridden with the `watermark_path` argument to `notion_source()`.
Pass `watermark_path=None` (default) to use the default, or
`watermark_path=False` to disable watermarking entirely (forces a full
block-fetch on every run - useful for testing or debugging).
"""

import json
import os
import pathlib
import tempfile
import time
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("notion_connector")

# dlt resource / staging-table name for Notion pages.
NOTION_TABLE_NAME = "notion_pages"
NOTION_SOURCE_NAME = "notion"
# Pin the Notion API version so upstream changes can't silently alter parsing.
_NOTION_VERSION = "2022-06-28"

# Retry budget for rate-limited / transient Notion API responses.
_MAX_RETRIES = 5

_EXTRA_HINT = (
    'The Notion connector requires the "notion" extra: pip install "cognee[notion]" '
    "(provides dlt and notion-client)."
)

_HEADING_PREFIX = {"heading_1": "# ", "heading_2": "## ", "heading_3": "### "}

# Public name of the default watermark file so callers can discover it.
NOTION_WATERMARK_DEFAULT_PATH = pathlib.Path.home() / ".cognee" / "notion_watermark.json"


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------


def notion_source(
    token=None,
    page_ids=None,
    database_ids=None,
    client=None,
    watermark_path=None,
):
    """Create a dlt source that yields Notion pages as markdown documents.

    Args:
        token: Notion integration token. Falls back to `NOTION_API_KEY`.
        page_ids: Restrict ingestion to these page ids. When omitted (and no
            `database_ids`), all pages the integration can see are searched.
        database_ids: Restrict ingestion to pages in these databases.
        client: Pre-built `notion_client.Client` (mainly a test-injection
            point); when omitted one is built from the token above.
        watermark_path: Path to the watermark JSON file used to skip unchanged
            pages' block-fetch on subsequent runs.

            * `None` (default): use `~/.cognee/notion_watermark.json`.
            * A `str` or `pathlib.Path`: use that path.
            * `False`: disable watermarking - always do a full block-fetch.

    Returns:
        A dlt source suitable for `cognee.add(...)` / `cognee.remember(...)`.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    if client is None:
        try:
            from notion_client import Client
        except ImportError as exc:
            raise ImportError(_EXTRA_HINT) from exc

        resolved_token = token or os.environ.get("NOTION_API_KEY")
        if not resolved_token:
            raise ValueError(
                "Notion integration token required: pass token= or set NOTION_API_KEY."
            )
        client = Client(auth=resolved_token, notion_version=_NOTION_VERSION)

    # Resolve the effective watermark path once, before the resource closure.
    if watermark_path is False:
        _wm_path = None
    elif watermark_path is None:
        _wm_path = NOTION_WATERMARK_DEFAULT_PATH
    else:
        _wm_path = pathlib.Path(watermark_path)

    @dlt.resource(name=NOTION_TABLE_NAME, primary_key="id", write_disposition="replace")
    def notion_pages():
        # Full-snapshot sync: each run replaces staging with exactly the pages
        # currently visible to the integration. Archived/trashed pages are
        # dropped from Notion's listings (and skipped below on the page_ids
        # path), so they fall out of staging and cognee's orphan_cleanup then
        # forgets them from the graph + vector stores. Unchanged pages keep a
        # stable content-hash data_id, so they are not re-ingested/re-cognified.
        #
        # A render error is NOT swallowed: because staging is authoritative
        # (replace), a page missing from a partial snapshot would be forgotten as
        # if deleted. Letting the error abort the run leaves staging - and memory
        # - untouched, which is the safe failure. Transient blips are already
        # retried in _request; only a persistent failure reaches here.
        #
        # Watermark: we load the previous run's store up-front, then accumulate
        # updates into pending_store throughout the loop.  Only when every page
        # has been processed successfully do we commit pending_store to disk.
        # A failure anywhere propagates the exception without touching the file.

        # Load the watermark from the previous run (empty dict = cold start).
        prev_store = _load_watermark(_wm_path) if _wm_path else {}
        # Accumulate updates here; written to disk only on success.
        pending_store = {}

        count = rendered = skipped = 0
        for page in _iter_pages(client, page_ids, database_ids):
            if page.get("archived") or page.get("in_trash"):
                continue

            count += 1
            page_id = page.get("id", "")
            last_edited = page.get("last_edited_time", "")

            cached = prev_store.get(page_id)
            if cached and cached.get("last_edited_time") == last_edited:
                # Page unchanged since last run - reuse cached markdown content.
                # The row is still yielded so the full-snapshot stays complete
                # (orphan_cleanup needs to see every live page every run).
                content = cached["content"]
                skipped += 1
                logger.debug(
                    "Notion: page %s unchanged (watermark hit), reusing cached content.",
                    page_id,
                )
            else:
                # Page is new or has been edited - fetch blocks from the API.
                content = _render_blocks(client, page_id)
                rendered += 1

            pending_store[page_id] = {
                "last_edited_time": last_edited,
                "content": content,
            }
            yield _page_to_row_from_content(page, content)

        logger.info(
            "Notion: synced %d page(s): %d rendered, %d skipped (watermark hit).",
            count,
            rendered,
            skipped,
        )

        # Commit the watermark only after all pages have been processed without
        # error.  If we reach here the snapshot is complete and safe to persist.
        if _wm_path is not None:
            _save_watermark(_wm_path, pending_store)
            logger.debug("Notion: watermark saved to %s.", _wm_path)

    @dlt.source(name=NOTION_SOURCE_NAME)
    def _notion():
        return notion_pages

    source = _notion()
    # Opt into the document ingestion path (page -> text document -> cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(source, DOCUMENT_SOURCE_ATTR, NOTION_SOURCE_NAME)
    return source


# ---------------------------------------------------------------------------
# Watermark helpers (module-private)
# ---------------------------------------------------------------------------


def _default_watermark_path():
    """Return the default watermark path, respecting COGNEE_HOME if set."""
    base = os.environ.get("COGNEE_HOME")
    if base:
        return pathlib.Path(base) / "notion_watermark.json"
    return NOTION_WATERMARK_DEFAULT_PATH


def _load_watermark(path):
    """Load the watermark store from *path*.

    Returns an empty dict when the file is missing, empty, or corrupt so a
    cold start is always a safe fallback to a full block-fetch.
    """
    if path is None:
        return {}
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8")
        data = json.loads(text)
        if isinstance(data, dict):
            return data
        logger.warning("Notion: watermark file %s has unexpected format, ignoring.", path)
        return {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Notion: could not read watermark %s (%s), ignoring.", path, exc)
        return {}


def _save_watermark(path, store):
    """Write *store* to *path* atomically (write-then-rename).

    Atomic rename ensures that a crash mid-write never leaves a corrupt file;
    the next run will simply see the previous good watermark.
    """
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write to a sibling temp file in the same directory so os.replace is
    # guaranteed to be atomic (same filesystem).
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".notion_wm_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        # Clean up the temp file on any error; re-raise so the caller knows.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Notion API helpers (module-private)
# ---------------------------------------------------------------------------


def _request(method, **kwargs):
    """Call a Notion API method, retrying rate-limit / transient errors.

    notion-client does not retry or honor `Retry-After` itself, and Notion
    enforces ~3 requests/second, so a page with many nested blocks would
    otherwise 429 and abort the sync. Rate-limit (429), server (5xx), timeout,
    and network errors are retried with backoff; permanent errors (auth,
    not-found) and exhausted retries propagate so the caller can decide.
    """
    for attempt in range(_MAX_RETRIES):
        try:
            return method(**kwargs)
        except Exception as exc:
            if attempt == _MAX_RETRIES - 1 or not _is_transient(exc):
                raise
            delay = _retry_after(getattr(exc, "headers", None), attempt)
            logger.warning(
                "Notion: %s - retrying in %.1fs (%d/%d).", exc, delay, attempt + 1, _MAX_RETRIES
            )
            time.sleep(delay)


def _is_transient(exc):
    """True for rate-limit / server / timeout / network errors worth retrying."""
    import httpx
    from notion_client.errors import HTTPResponseError, RequestTimeoutError

    if isinstance(exc, (RequestTimeoutError, httpx.TransportError)):
        return True
    # APIResponseError subclasses HTTPResponseError; both expose .status.
    if isinstance(exc, HTTPResponseError):
        return getattr(exc, "status", None) in (429, 500, 502, 503, 504)
    return False


def _is_gone(exc):
    """True when a page is permanently gone / not shared (forgetting is correct)."""
    from notion_client.errors import APIResponseError

    return isinstance(exc, APIResponseError) and getattr(exc, "status", None) in (403, 404)


def _retry_after(headers, attempt):
    """Seconds to wait before retrying: the Retry-After header, else backoff."""
    header = (headers or {}).get("retry-after") or (headers or {}).get("Retry-After")
    try:
        return float(header)
    except (TypeError, ValueError):
        return float(2**attempt)


def _iter_pages(client, page_ids, database_ids):
    """Yield raw Notion page objects for the configured scope."""
    if page_ids:
        for page_id in page_ids:
            try:
                yield _request(client.pages.retrieve, page_id=page_id)
            except Exception as exc:
                # A permanently-gone page is skipped so it gets forgotten; a
                # transient error re-raises (a partial snapshot must not drive
                # deletions under replace).
                if _is_gone(exc):
                    logger.warning("Notion: page %s is gone, skipping: %s", page_id, exc)
                    continue
                raise
        return

    if database_ids:
        for database_id in database_ids:
            yield from _paginate(client.databases.query, database_id=database_id)
        return

    # No explicit scope: search every page the integration can see.
    yield from _paginate(client.search, filter={"property": "object", "value": "page"})


def _paginate(method, **kwargs):
    """Yield results across Notion's cursor-based pagination."""
    cursor = None
    while True:
        response = (
            _request(method, start_cursor=cursor, **kwargs)
            if cursor
            else _request(method, **kwargs)
        )
        yield from response.get("results", [])
        cursor = response.get("next_cursor")
        # Stop on the last page, or if Notion signals "more" without a cursor
        # (contract violation) so we can't loop forever.
        if not response.get("has_more") or not cursor:
            return


def _page_to_row(client, page):
    """Flatten a Notion page + its block children into a document row.

    Only `title`/`content` (+ `id`/`url` for identity and provenance)
    are kept, so a metadata-only edit that bumps `last_edited_time` without
    changing the text does not churn the content-hash data_id.
    """
    return _page_to_row_from_content(page, _render_blocks(client, page.get("id")))


def _page_to_row_from_content(page, content):
    """Build a document row from a pre-rendered content string.

    Separating content rendering from row construction lets the watermark
    path reuse cached content without calling `_render_blocks` again.
    """
    return {
        "id": page.get("id"),
        "url": page.get("url"),
        "title": _page_title(page),
        "content": content,
    }


def _page_title(page):
    """Extract the page title from its title property."""
    properties = page.get("properties") or {}
    for prop in properties.values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            return _rich_text(prop.get("title"))
    return ""


def _render_blocks(client, block_id, depth=0):
    """Render a block's children to markdown, recursing into nested blocks."""
    # Guard against pathological nesting / cycles.
    if not block_id or depth > 10:
        return ""

    lines = []
    for block in _paginate(client.blocks.children.list, block_id=block_id):
        rendered = _render_block(block)
        if rendered:
            lines.append(rendered)
        if block.get("has_children"):
            nested = _render_blocks(client, block.get("id"), depth + 1)
            if nested:
                lines.append(nested)

    return "\n".join(lines)


def _render_block(block):
    """Render a single Notion block to a markdown line."""
    block_type = block.get("type")
    if not block_type:
        return ""

    payload = block.get(block_type) or {}
    text = _rich_text(payload.get("rich_text"))

    if block_type in _HEADING_PREFIX:
        return f"{_HEADING_PREFIX[block_type]}{text}" if text else ""
    if block_type == "bulleted_list_item":
        return f"- {text}" if text else ""
    if block_type == "numbered_list_item":
        return f"1. {text}" if text else ""
    if block_type == "to_do":
        checked = "x" if payload.get("checked") else " "
        return f"- [{checked}] {text}" if text else ""
    if block_type == "code":
        language = payload.get("language") or ""
        fence = "```"
        return f"{fence}{language}\n{text}\n{fence}" if text else ""

    # Paragraph, quote, callout, toggle, and any other rich_text block render as
    # their plain text.
    return text


def _rich_text(rich_text):
    """Concatenate the plain_text of a Notion rich_text array."""
    if not isinstance(rich_text, list):
        return ""
    return "".join(part.get("plain_text", "") for part in rich_text if isinstance(part, dict))
