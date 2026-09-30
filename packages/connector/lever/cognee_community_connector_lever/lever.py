"""Lever connector for cognee — a ``dlt`` source that turns your ATS into memory.

Pull Lever job postings (and, opt-in, interview feedback and notes) into cognee,
incrementally and with forget-on-deletion — "ask my hiring pipeline"::

    import cognee
    from cognee_community_connector_lever import lever_source

    await cognee.remember(
        lever_source(),              # LEVER_API_KEY from env, or api_key="..."
        dataset_name="lever",
        primary_key="id",
        write_disposition="merge",   # REQUIRED (see .. important:: below)
        max_rows_per_table=0,
    )

.. important::
   ``write_disposition="merge"`` is **mandatory**. The add pipeline defaults to
   ``"replace"``; on the second, incremental sync that would drop every posting
   that did not change since the previous run.

Design
------
* **Auth** — a Lever API key, sent as the HTTP Basic-auth username with a blank
  password (Lever's documented scheme). The connector only issues ``GET``s, so
  a key with read-only permissions is enough.
* **Document mode** — each row is ``{id, title, content, url}`` and the resource
  declares ``cognee_document_source = "lever"``, so postings and feedback flow
  through normal cognify (LLM entity extraction) rather than the relational
  dlt-row path. Only those columns are kept, so a metadata-only change does
  not churn the content-hash ``data_id``.
* **Incremental cursor** — Lever's ``updated_at_start`` filter on ``/postings``
  and ``/opportunities``. The cursor is the wall-clock time captured at the
  *start* of the previous run (minus a small overlap), persisted in dlt's
  per-resource state, so anything edited mid-sync is picked up next time.
  Re-emitting an unchanged record is harmless under ``merge``.
* **Forget-on-delete** — Lever exposes delete feeds (``/postings/deleted`` and
  ``/opportunities/deleted``). Ids from those feeds, and records that moved out
  of the selected scope (e.g. a posting whose state is no longer selected, or
  that became confidential), are emitted with the ``_deleted`` hard-delete
  marker. dlt drops those rows on ``merge`` and cognee's existing
  ``orphan_cleanup`` purges them from the graph + vector + relational stores.

Restricted candidate data
-------------------------
Candidate information is treated as **restricted by default**:

* Only postings are ingested unless you explicitly pass ``include_feedback=True``
  and/or ``include_notes=True``.
* Even then, opportunity documents carry **no candidate contact data** — no
  name, email, phone, links, headline or location. Only the free text that
  interviewers wrote (feedback answers, non-secret notes) is ingested, keyed by
  the opportunity id.
* Secret notes, deleted forms, and confidential postings/opportunities are
  skipped (``include_confidential=True`` opts confidential records back in).

.. note::
   Lever does not bump an opportunity's ``updatedAt`` for every feedback/notes
   edit, only for the profile fields it documents (stage, tags, archived,
   ``lastInteractionAt``, …). Feedback or a note that changes without touching
   those is picked up the next time the opportunity itself is updated. Pass
   ``full_refresh=True`` to re-read everything in scope.
"""

from __future__ import annotations

import html
import os
import re
import time
from collections.abc import Callable, Iterable, Iterator
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("lever_connector")

LEVER_SOURCE_NAME = "lever"
LEVER_TABLE_NAME = "lever_documents"
LEVER_API_BASE = "https://api.lever.co/v1"

# Lever pages accept 1..100 items.
_PAGE_LIMIT = 100
# Retry budget for 429 / 5xx / network errors. Lever allows ~10 req/s per key.
_MAX_RETRIES = 5
# Re-read a small window before the cursor so clock skew or a record written
# at the cursor boundary is never missed. Duplicates are idempotent under merge.
_CURSOR_OVERLAP_MS = 5 * 60 * 1000
# /postings/deleted rejects windows longer than 30 days; stay safely under it.
_DELETED_WINDOW_MS = 29 * 24 * 60 * 60 * 1000

# Indirection so tests can skip real backoff waits.
_sleep = time.sleep

_EXTRA_HINT = (
    'The Lever connector requires dlt and requests: pip install "cognee-community-connector-lever".'
)

_LI_RE = re.compile(r"<li[^>]*>", re.IGNORECASE)
_BLOCK_RE = re.compile(r"</(p|div|li|ul|ol|h[1-6])>|<br\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACES_RE = re.compile(r"[ \t\r\f\v]+")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def _make_session(api_key: str) -> Any:
    """Build a ``requests`` session authenticated with a Lever API key."""
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - depends on installed extras
        raise ImportError(_EXTRA_HINT) from exc

    session = requests.Session()
    # Lever: API key as the Basic-auth username, blank password.
    session.auth = (api_key, "")
    session.headers.update({"Accept": "application/json"})
    return session


def _is_transient_status(status: int | None) -> bool:
    return status in (429, 500, 502, 503, 504)


def _retry_after(headers: Any, attempt: int) -> float:
    """Seconds to wait before retrying: the Retry-After header, else backoff."""
    header = None
    if headers:
        header = headers.get("Retry-After") or headers.get("retry-after")
    try:
        return float(header)
    except (TypeError, ValueError):
        return float(2**attempt)


def _api_get(session: Any, path: str, params: dict | None = None) -> dict:
    """GET a Lever API path and return JSON, retrying rate-limit / transient errors.

    Permanent errors (401/403/404, …) and exhausted retries propagate, which
    aborts the run *before* the cursor advances — so nothing is skipped.
    """
    url = path if path.startswith("http") else f"{LEVER_API_BASE}{path}"
    for attempt in range(_MAX_RETRIES):
        try:
            response = session.get(url, params=params or {})
        except Exception as exc:
            if attempt == _MAX_RETRIES - 1 or not _is_network_error(exc):
                raise
            delay = float(2**attempt)
            logger.warning("Lever: %s — retrying in %.1fs.", exc, delay)
            _sleep(delay)
            continue

        status = getattr(response, "status_code", 200)
        if _is_transient_status(status) and attempt < _MAX_RETRIES - 1:
            delay = _retry_after(getattr(response, "headers", None), attempt)
            logger.warning(
                "Lever: HTTP %s on %s — retrying in %.1fs (%d/%d).",
                status,
                path,
                delay,
                attempt + 1,
                _MAX_RETRIES,
            )
            _sleep(delay)
            continue

        response.raise_for_status()
        return response.json()

    raise RuntimeError(f"Lever: exhausted retries for {path}")  # pragma: no cover


def _is_network_error(exc: Exception) -> bool:
    try:
        import requests
    except ImportError:  # pragma: no cover
        return False
    return isinstance(exc, (requests.ConnectionError, requests.Timeout))


def _paginate(session: Any, path: str, params: dict | None = None) -> Iterator[dict]:
    """Yield ``data`` items across Lever's offset-token pagination."""
    base_params = dict(params or {})
    base_params["limit"] = _PAGE_LIMIT
    offset: str | None = None
    while True:
        page_params = dict(base_params)
        if offset:
            page_params["offset"] = offset
        payload = _api_get(session, path, page_params)
        yield from payload.get("data") or []
        offset = payload.get("next")
        # Stop on the last page, or if Lever says "more" without a token
        # (contract violation) so we can never loop forever.
        if not payload.get("hasNext") or not offset:
            return


def _deleted_ids(
    session: Any, path: str, start_ms: int, end_ms: int, *, window_ms: int | None
) -> Iterator[str]:
    """Yield ids from a Lever ``/…/deleted`` feed between ``start_ms`` and ``end_ms``.

    ``window_ms`` splits the range into chunks for endpoints that cap the
    window (``/postings/deleted`` allows at most 30 days per request).
    """
    if start_ms >= end_ms:
        return
    step = window_ms or (end_ms - start_ms)
    chunk_start = start_ms
    while chunk_start < end_ms:
        chunk_end = min(chunk_start + step, end_ms)
        params = {"deleted_at_start": chunk_start, "deleted_at_end": chunk_end}
        for item in _paginate(session, path, params):
            item_id = item.get("id")
            if item_id:
                yield str(item_id)
        chunk_start = chunk_end


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _html_to_text(raw: Any) -> str:
    """Turn Lever's styled HTML (list items, paragraphs) into plain text.

    ``<li>`` becomes a ``- `` bullet so requirement lists stay readable.
    Dependency-free on purpose — no HTML parser required.
    """
    if not raw or not isinstance(raw, str):
        return ""
    text = _LI_RE.sub("\n- ", raw)
    text = _BLOCK_RE.sub("\n", text)
    text = html.unescape(_TAG_RE.sub("", text))
    lines = [_SPACES_RE.sub(" ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line and line != "-")


def _format_value(value: Any) -> str:
    """Render a Lever form-field value (text, score, list, scorecard…) as text."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = [_format_value(item) for item in value]
        return ", ".join(part for part in parts if part)
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            rendered = _format_value(item)
            if rendered:
                parts.append(f"{key}: {rendered}")
        return "; ".join(parts)
    return str(value)


def _posting_to_row(posting: dict) -> dict[str, Any]:
    """Flatten a Lever posting into a document row."""
    content = posting.get("content") or {}
    categories = posting.get("categories") or {}

    facts = []
    for label, value in (
        ("Team", categories.get("team")),
        ("Department", categories.get("department")),
        ("Location", categories.get("location")),
        ("Commitment", categories.get("commitment")),
        ("Workplace", posting.get("workplaceType")),
        ("State", posting.get("state")),
    ):
        if value:
            facts.append(f"{label}: {value}")

    sections: list[str] = []
    if facts:
        sections.append("\n".join(facts))
    tags = [str(tag) for tag in (posting.get("tags") or []) if tag]
    if tags:
        sections.append("Tags: " + ", ".join(tags))

    description = content.get("description") or _html_to_text(content.get("descriptionHtml"))
    if description:
        sections.append(description.strip())

    for lst in content.get("lists") or []:
        body = _html_to_text(lst.get("content"))
        heading = (lst.get("text") or "").strip()
        if heading and body:
            sections.append(f"## {heading}\n{body}")
        elif body:
            sections.append(body)

    closing = content.get("closing") or _html_to_text(content.get("closingHtml"))
    if closing:
        sections.append(closing.strip())

    urls = posting.get("urls") or {}
    return {
        "id": _posting_key(posting["id"]),
        "title": (posting.get("text") or "").strip(),
        "content": "\n\n".join(sections),
        "url": urls.get("show") or None,
        "_deleted": False,
    }


def _render_forms(forms: Iterable[dict], heading: str) -> list[str]:
    """Render feedback / note forms, skipping deleted and secret ones."""
    blocks: list[str] = []
    for form in forms:
        if form.get("deletedAt") or form.get("secret"):
            continue
        lines = []
        for field in form.get("fields") or []:
            answer = _format_value(field.get("value"))
            if not answer:
                continue
            question = (field.get("text") or "").strip()
            lines.append(f"- {question}: {answer}" if question else f"- {answer}")
        if not lines:
            continue
        title = (form.get("text") or "").strip()
        header = f"## {heading}: {title}" if title else f"## {heading}"
        blocks.append(header + "\n" + "\n".join(lines))
    return blocks


def _opportunity_to_row(
    session: Any, opportunity: dict, *, include_feedback: bool, include_notes: bool
) -> dict[str, Any] | None:
    """Build the (restricted) activity document for one opportunity.

    Deliberately carries **no candidate contact data**: only interviewer-written
    feedback answers and non-secret notes, keyed by the opportunity id.
    Returns ``None`` when there is nothing to ingest.
    """
    opportunity_id = str(opportunity["id"])
    blocks: list[str] = []
    if include_feedback:
        forms = _paginate(session, f"/opportunities/{opportunity_id}/feedback")
        blocks.extend(_render_forms(forms, "Feedback"))
    if include_notes:
        notes = _paginate(session, f"/opportunities/{opportunity_id}/notes")
        blocks.extend(_render_forms(notes, "Note"))
    if not blocks:
        return None

    urls = opportunity.get("urls") or {}
    return {
        "id": _opportunity_key(opportunity_id),
        "title": f"Lever candidate feedback (opportunity {opportunity_id})",
        "content": "\n\n".join(blocks),
        "url": urls.get("show") or None,
        "_deleted": False,
    }


def _posting_key(posting_id: Any) -> str:
    return f"posting:{posting_id}"


def _opportunity_key(opportunity_id: Any) -> str:
    return f"opportunity:{opportunity_id}"


def _tombstone(key: str) -> dict[str, Any]:
    """A minimal row that instructs dlt to hard-delete a document by id."""
    return {"id": key, "_deleted": True}


def _is_confidential(record: dict) -> bool:
    return record.get("confidentiality") == "confidential"


# ---------------------------------------------------------------------------
# Sync (pure given a session + state dict — unit-testable)
# ---------------------------------------------------------------------------
def sync_postings(
    session: Any,
    state: dict,
    *,
    now_ms: int,
    posting_states: list[str] | None = None,
    include_confidential: bool = False,
    full_refresh: bool = False,
) -> Iterator[dict[str, Any]]:
    """Yield postings changed since the cursor, plus hard-delete tombstones.

    The first run (or ``full_refresh``) lists every posting; later runs pass
    ``updated_at_start`` and read ``/postings/deleted`` for removals. The cursor
    only advances once the generator is fully consumed, so a failed run is
    retried from the same point.
    """
    # Tombstones only matter once something may have been ingested before.
    previous_cursor = state.get("postings_cursor")
    synced_before = previous_cursor is not None
    cursor = None if full_refresh else previous_cursor
    params: dict[str, Any] = {}
    if cursor is not None:
        params["updated_at_start"] = max(0, int(cursor) - _CURSOR_OVERLAP_MS)

    wanted_states = set(posting_states or [])
    changed = removed = 0
    for posting in _paginate(session, "/postings", params):
        in_scope = (not wanted_states or posting.get("state") in wanted_states) and (
            include_confidential or not _is_confidential(posting)
        )
        if in_scope:
            changed += 1
            yield _posting_to_row(posting)
        elif synced_before:
            # Moved out of scope (state changed, became confidential): forget it.
            # Tombstoning an id that was never ingested is a no-op under merge.
            removed += 1
            yield _tombstone(_posting_key(posting["id"]))

    if synced_before:
        # The delete feed is read from the *stored* cursor even on full_refresh:
        # a re-listing cannot reveal records that no longer exist.
        start = max(0, int(previous_cursor) - _CURSOR_OVERLAP_MS)
        for posting_id in _deleted_ids(
            session, "/postings/deleted", start, now_ms, window_ms=_DELETED_WINDOW_MS
        ):
            removed += 1
            yield _tombstone(_posting_key(posting_id))

    state["postings_cursor"] = now_ms
    logger.info("Lever: %d posting(s) synced, %d removed.", changed, removed)


def sync_opportunities(
    session: Any,
    state: dict,
    *,
    now_ms: int,
    include_feedback: bool,
    include_notes: bool,
    posting_ids: list[str] | None = None,
    include_confidential: bool = False,
    full_refresh: bool = False,
) -> Iterator[dict[str, Any]]:
    """Yield feedback/notes documents for opportunities updated since the cursor.

    Deleted opportunities (``/opportunities/deleted``) and ones that became
    confidential or lost all ingestible content are emitted as tombstones.
    """
    previous_cursor = state.get("opportunities_cursor")
    synced_before = previous_cursor is not None
    cursor = None if full_refresh else previous_cursor
    params: dict[str, Any] = {}
    if posting_ids:
        # Repeated query param (posting_id=a&posting_id=b): union of postings.
        params["posting_id"] = list(posting_ids)
    if cursor is not None:
        params["updated_at_start"] = max(0, int(cursor) - _CURSOR_OVERLAP_MS)

    changed = removed = 0
    for opportunity in _paginate(session, "/opportunities", params):
        key = _opportunity_key(opportunity["id"])
        row = None
        if include_confidential or not _is_confidential(opportunity):
            row = _opportunity_to_row(
                session,
                opportunity,
                include_feedback=include_feedback,
                include_notes=include_notes,
            )
        if row is not None:
            changed += 1
            yield row
        elif synced_before:
            # No (longer any) ingestible content, or became confidential.
            removed += 1
            yield _tombstone(key)

    if synced_before:
        # The delete feed is read from the *stored* cursor even on full_refresh:
        # a re-listing cannot reveal records that no longer exist.
        start = max(0, int(previous_cursor) - _CURSOR_OVERLAP_MS)
        for opportunity_id in _deleted_ids(
            session, "/opportunities/deleted", start, now_ms, window_ms=None
        ):
            removed += 1
            yield _tombstone(_opportunity_key(opportunity_id))

    state["opportunities_cursor"] = now_ms
    logger.info("Lever: %d opportunity document(s) synced, %d removed.", changed, removed)


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def lever_source(
    api_key: str | None = None,
    *,
    include_postings: bool = True,
    include_feedback: bool = False,
    include_notes: bool = False,
    posting_states: list[str] | None = None,
    posting_ids: list[str] | None = None,
    include_confidential: bool = False,
    full_refresh: bool = False,
    session: Any = None,
    clock: Callable[[], int] | None = None,
):
    """Return a ``dlt`` resource that yields Lever documents for ``remember``.

    Args:
        api_key: Lever API key. Falls back to ``LEVER_API_KEY``.
        include_postings: Ingest job postings (default on).
        include_feedback: Ingest interview feedback forms (restricted — off by
            default; see the module docstring).
        include_notes: Ingest non-secret candidate notes (restricted — off by
            default).
        posting_states: Only keep postings in these states, e.g.
            ``["published", "internal"]``. ``None`` keeps every state.
        posting_ids: Only sync feedback/notes for opportunities applied to these
            postings. Recommended for large accounts.
        include_confidential: Also ingest confidential postings / opportunities.
        full_refresh: Ignore the stored cursors and re-read everything in scope.
        session: Pre-built ``requests``-like session (test injection point).
        clock: Returns "now" in epoch ms (test injection point).

    Returns:
        A ``dlt`` resource (``lever_documents``) with ``primary_key="id"``,
        ``write_disposition="merge"`` and an ``_deleted`` hard-delete column.
        Hand it to ``cognee.remember(..., write_disposition="merge")``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    if not (include_postings or include_feedback or include_notes):
        raise ValueError(
            "Nothing to ingest: enable at least one of include_postings, "
            "include_feedback or include_notes."
        )

    resolved_key = api_key or os.environ.get("LEVER_API_KEY")
    if session is None and not resolved_key:
        raise ValueError("Lever API key required: pass api_key= or set LEVER_API_KEY.")

    now = clock or (lambda: int(time.time() * 1000))

    @dlt.resource(
        name=LEVER_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        # _deleted is a boolean hard-delete marker: rows where it is True are
        # removed from the dlt destination on merge, which propagates the
        # deletion through cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def lever_documents():
        client = session or _make_session(resolved_key)
        state = dlt.current.resource_state()
        # Captured before any listing so records edited mid-sync are re-read
        # on the next run instead of falling between two cursors.
        run_started_ms = now()
        if include_postings:
            yield from sync_postings(
                client,
                state,
                now_ms=run_started_ms,
                posting_states=posting_states,
                include_confidential=include_confidential,
                full_refresh=full_refresh,
            )
        if include_feedback or include_notes:
            yield from sync_opportunities(
                client,
                state,
                now_ms=run_started_ms,
                include_feedback=include_feedback,
                include_notes=include_notes,
                posting_ids=posting_ids,
                include_confidential=include_confidential,
                full_refresh=full_refresh,
            )

    resource = lever_documents()
    # Opt into the document ingestion path: each row (id/title/content/url)
    # becomes a text document that goes through normal cognify.
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(resource, DOCUMENT_SOURCE_ATTR, LEVER_SOURCE_NAME)
    return resource
