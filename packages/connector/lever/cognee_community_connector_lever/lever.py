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
* Interviewers often write the candidate's name in that free text, so the
  candidate's name (and each part of it), emails and phone numbers are replaced
  with a stable pseudonym such as ``Candidate 3f2a9c1e``. The pseudonym is a
  one-way hash of the Lever contact id: it keeps one person's documents linked
  without revealing who they are.
* Secret notes, deleted forms, and confidential postings/opportunities are
  skipped (``include_confidential=True`` opts confidential records back in).
* Anonymized candidates (``isAnonymized``, e.g. after a GDPR erasure request)
  are forgotten on the next sync.

Feedback and notes freshness
----------------------------
Lever does not bump an opportunity's ``updatedAt`` when feedback or a note is
added, edited or deleted. Scope the sync with ``posting_ids`` (recommended):
every opportunity on those postings is then re-read on each run, so new
feedback is never missed and unchanged documents are not re-cognified. Without
``posting_ids`` the connector stays account-wide incremental, and feedback on an
otherwise untouched opportunity is picked up on its next update or with
``full_refresh=True``.
"""

from __future__ import annotations

import hashlib
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


def _candidate_label(opportunity: dict) -> str:
    """A stable pseudonym such as ``Candidate 3f2a9c1e`` for one candidate.

    Derived from the Lever contact id (one person across opportunities), falling
    back to the opportunity id. It is a one-way hash, so the label keeps a
    candidate's documents linked in the graph without revealing who they are,
    and different candidates never collapse into one "candidate" entity.
    """
    basis = str(opportunity.get("contact") or opportunity["id"])
    return "Candidate " + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:8]


def _identifying_strings(opportunity: dict) -> list[str]:
    """Candidate identifiers to scrub from free text, longest first.

    Covers the full name, each name part (so "Jane" and "Jane's" are caught as
    well as "Jane Doe"), email addresses and phone numbers.
    """
    values: set[str] = set()
    name = opportunity.get("name")
    if isinstance(name, str) and name.strip():
        values.add(name.strip())
        for part in name.split():
            part = part.strip(".,()\"'")
            # Skip initials ("Q.") — too short to match safely.
            if len(part) >= 2 and any(ch.isalpha() for ch in part):
                values.add(part)
    for email in opportunity.get("emails") or []:
        if isinstance(email, str) and email.strip():
            values.add(email.strip())
    for phone in opportunity.get("phones") or []:
        value = phone.get("value") if isinstance(phone, dict) else phone
        if isinstance(value, str) and value.strip():
            values.add(value.strip())
    return sorted(values, key=len, reverse=True)


def _redact(text: str, identifiers: list[str], label: str) -> str:
    """Replace every identifier in ``text`` with ``label`` (case-insensitive).

    Matches whole words only, so redacting "Jane" leaves "Janet" untouched.
    Errs on the side of privacy: a name part that is also a common word is
    still replaced.
    """
    if not identifiers:
        return text
    alternatives = "|".join(re.escape(value) for value in identifiers)
    pattern = re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", re.IGNORECASE)
    return pattern.sub(label, text)


def _opportunity_to_row(
    session: Any, opportunity: dict, *, include_feedback: bool, include_notes: bool
) -> dict[str, Any] | None:
    """Build the (restricted) activity document for one opportunity.

    Deliberately carries **no candidate contact data**: only interviewer-written
    feedback answers and non-secret notes, keyed by the opportunity id. The
    candidate's name, emails and phone numbers are also scrubbed from that free
    text and replaced by a stable pseudonym (see ``_candidate_label``).
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

    label = _candidate_label(opportunity)
    content = _redact("\n\n".join(blocks), _identifying_strings(opportunity), label)
    urls = opportunity.get("urls") or {}
    return {
        "id": _opportunity_key(opportunity_id),
        "title": f"Lever interview feedback for {label}",
        "content": content,
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


def _is_anonymized(opportunity: dict) -> bool:
    """True once Lever has anonymized the candidate (e.g. a GDPR erasure)."""
    return bool(opportunity.get("isAnonymized"))


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
    """Yield feedback/notes documents for opportunities, plus tombstones.

    Two modes, because Lever does not bump an opportunity's ``updatedAt`` when
    feedback or a note is added, edited or deleted:

    * **Scoped rescan** (``posting_ids`` given): every opportunity on those
      postings is re-read each run, so new or changed feedback is never missed.
      Unchanged documents render to identical content and keep their content-hash
      ``data_id``, so nothing is re-cognified. Opportunities that drop out of the
      scope since the last run are tombstoned (tracked in ``state``).
    * **Account-wide incremental** (no ``posting_ids``): only opportunities whose
      ``updatedAt`` moved since the cursor are re-read, which bounds the cost on
      large accounts; feedback on an otherwise untouched opportunity waits for
      its next update (or ``full_refresh=True``).

    In both modes, deleted opportunities (``/opportunities/deleted``) and ones
    that became confidential, were anonymized, or lost all ingestible content
    are emitted as tombstones.
    """
    previous_cursor = state.get("opportunities_cursor")
    synced_before = previous_cursor is not None
    rescan = bool(posting_ids)
    cursor = None if (full_refresh or rescan) else previous_cursor
    params: dict[str, Any] = {}
    if posting_ids:
        # Repeated query param (posting_id=a&posting_id=b): union of postings.
        params["posting_id"] = list(posting_ids)
    if cursor is not None:
        params["updated_at_start"] = max(0, int(cursor) - _CURSOR_OVERLAP_MS)

    # Each id is emitted at most once per run, so a record can never be both
    # upserted and tombstoned (or tombstoned twice) in the same load.
    emitted: set[str] = set()
    seen_ids: set[str] = set()
    changed = removed = 0
    for opportunity in _paginate(session, "/opportunities", params):
        opportunity_id = str(opportunity["id"])
        key = _opportunity_key(opportunity_id)
        if key in emitted:
            continue
        seen_ids.add(opportunity_id)
        row = None
        # Anonymized (GDPR erasure) and confidential records are never read;
        # whatever was ingested for them before is forgotten below.
        if not _is_anonymized(opportunity) and (
            include_confidential or not _is_confidential(opportunity)
        ):
            row = _opportunity_to_row(
                session,
                opportunity,
                include_feedback=include_feedback,
                include_notes=include_notes,
            )
        if row is not None:
            changed += 1
            emitted.add(key)
            yield row
        elif synced_before:
            # No (longer any) ingestible content, became confidential, or was
            # anonymized. Tombstoning a never-ingested id is a no-op under merge.
            removed += 1
            emitted.add(key)
            yield _tombstone(key)

    if rescan:
        known_ids = set(state.get("opportunity_ids") or [])
        if known_ids and not seen_ids:
            # An empty listing while opportunities were known almost always means
            # a transient failure or a mistyped posting id, not a real wipe.
            # Forgetting everything would be permanent, so skip it this run.
            logger.warning(
                "Lever: opportunity listing returned nothing but %d were known; "
                "skipping scope-based forgetting this run.",
                len(known_ids),
            )
        else:
            for opportunity_id in sorted(known_ids - seen_ids):
                key = _opportunity_key(opportunity_id)
                if key not in emitted:
                    removed += 1
                    emitted.add(key)
                    yield _tombstone(key)
            state["opportunity_ids"] = sorted(seen_ids)
    else:
        # Switching back to account-wide mode: the scoped id set no longer applies.
        state.pop("opportunity_ids", None)

    if synced_before:
        # The delete feed is read from the *stored* cursor even on full_refresh:
        # a re-listing cannot reveal records that no longer exist.
        start = max(0, int(previous_cursor) - _CURSOR_OVERLAP_MS)
        for opportunity_id in _deleted_ids(
            session, "/opportunities/deleted", start, now_ms, window_ms=None
        ):
            key = _opportunity_key(opportunity_id)
            if key not in emitted:
                removed += 1
                emitted.add(key)
                yield _tombstone(key)

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
            postings. Recommended: in this mode every in-scope opportunity is
            re-read each run, so new feedback/notes are never missed.
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
