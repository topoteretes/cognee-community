"""Close CRM connector for cognee — a ``dlt`` source that turns Close data into memory.

Sync leads, opportunities, notes, call summaries, and emails from Close CRM
into cognee, incrementally and with forget-on-deletion.  The resource produced
here is handed directly to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_close import close_source

    await cognee.remember(
        close_source(selected_types=["lead", "opportunity", "note", "call"]),
        dataset_name="sales_memory",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

Design
------
* **Auth** — API key via HTTP Basic auth (key as username, empty password),
  read from ``CLOSE_API_KEY`` or passed in.  Validated when the source is
  built, not halfway through a pipeline run.
* **Primary key** — the Close object ``id`` (globally unique, prefixed by type:
  ``lead_``, ``oppo_``, ``acti_``).  With ``write_disposition="merge"`` this
  gives idempotent upserts.
* **Incremental sync** — each run does a full reconciliation: every object in
  the selected scope is fetched, rendered, and hashed.  Only objects whose
  content hash changed since the last run are yielded — unchanged objects are
  skipped.  State (``{object_id: {type, hash}}``) is persisted in dlt's
  per-resource state.
* **Forget-on-delete** — each run also diffs the current set of object ids
  against the previous state.  Missing ids become ``_deleted`` tombstones that
  dlt drops on ``merge``, and cognee's ``orphan_cleanup`` purges them from the
  graph, vector, and relational stores.  A partial or failed listing aborts the
  run before any tombstone is emitted, so transient errors never cause
  accidental forgetting.
* **Content** — call summaries, transcripts, and email bodies are the valuable
  parts and live on separate endpoints from the lead.  They are explicitly
  requested via ``_fields`` and rendered into the document, with bounded size.
  Each object is its own document so an edited call doesn't re-cognify the
  whole lead, and a deleted activity can be removed on its own.
* **Document path** — the resource declares ``cognee_document_source = "close"``
  so each row flows through normal cognify (LLM graph extraction) rather than
  the deterministic dlt-row schema-context path.

Limitations
-----------
* The Event Log only reaches back 30 days and is not used for change
  discovery.  Every sync is a full reconciliation; this is reliable but
  re-fetches all objects each run.  An Event-Log-assisted optimization can
  reduce API calls in a follow-up.
* ``_fields`` filtering for calls and emails is explicit; a new field added by
  Close must be added to the fields list to be ingested.
"""

import hashlib
import os
import re
import time
from collections.abc import Callable
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("close_connector")

CLOSE_BASE_URL = "https://api.close.com/api/v1"
CLOSE_SOURCE_NAME = "close"

# Retry budget for rate-limited / transient Close API responses.
_MAX_RETRIES = 5
_PAGE_SIZE = 100

# Bound transcript and email body size to avoid oversized documents.
_MAX_TRANSCRIPT_CHARS = 10_000
_MAX_BODY_CHARS = 10_000

# Close object types and their API list endpoints.
OBJECT_PATHS = {
    "lead": "/lead/",
    "opportunity": "/opportunity/",
    "note": "/activity/note/",
    "call": "/activity/call/",
    "email": "/activity/email/",
}

ALL_TYPES = tuple(OBJECT_PATHS)
DEFAULT_TYPES = ("lead", "opportunity", "note", "call")

# Activity types that reference a parent lead.
_ACTIVITY_TYPES = frozenset({"opportunity", "note", "call", "email"})

# Fields to explicitly request for calls and emails.  Close does not return
# recording_transcript / voicemail_transcript / body_text by default; they must
# be requested via the ``_fields`` query parameter.
_CALL_FIELDS = [
    "id", "lead_id", "direction", "disposition", "duration",
    "date_created", "date_updated", "summary", "note",
    "recording_transcript", "voicemail_transcript",
]
_EMAIL_FIELDS = [
    "id", "lead_id", "subject", "direction", "sender", "to",
    "date_created", "date_updated", "body_text", "body_html",
]

# Per-type field overrides; None = use default (all) fields.
_EXTRA_FIELDS: dict[str, list[str] | None] = {
    "lead": None,
    "opportunity": None,
    "note": None,
    "call": _CALL_FIELDS,
    "email": _EMAIL_FIELDS,
}


# ---------------------------------------------------------------------------
# HTTP client with retry and pagination
# ---------------------------------------------------------------------------


class _CloseClient:
    """Thin Close REST API wrapper with Basic-auth, retry, and pagination."""

    def __init__(self, http_client: Any, api_key: str):
        self._http = http_client
        self._api_key = api_key

    def request(self, method: str, path: str, *, params: dict | None = None) -> dict:
        """Make an API request with retry for 429 / 5xx / transient errors."""
        import httpx

        url = f"{CLOSE_BASE_URL}{path}"
        auth = httpx.BasicAuth(self._api_key, "")
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                resp = self._http.request(method, url, params=params, auth=auth)
                if resp.status_code in (429,) or resp.status_code >= 500:
                    if attempt < _MAX_RETRIES - 1:
                        delay = _retry_delay(resp, attempt)
                        logger.warning(
                            "Close: %d — retrying in %.1fs (%d/%d).",
                            resp.status_code, delay, attempt + 1, _MAX_RETRIES,
                        )
                        time.sleep(delay)
                        continue
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:
                last_exc = exc
                if attempt == _MAX_RETRIES - 1 or not _is_transient(exc):
                    raise
                delay = float(2**attempt)
                logger.warning(
                    "Close: %s — retrying in %.1fs (%d/%d).",
                    exc, delay, attempt + 1, _MAX_RETRIES,
                )
                time.sleep(delay)
        # Unreachable under normal control flow, but satisfies type-checkers.
        raise RuntimeError("Close: retries exhausted") from last_exc  # pragma: no cover

    def iter_objects(self, path: str, *, fields: list[str] | None = None):
        """Paginate through a Close list endpoint using _skip / _limit."""
        skip = 0
        while True:
            params: dict[str, Any] = {"_skip": skip, "_limit": _PAGE_SIZE}
            if fields:
                params["_fields"] = ",".join(fields)
            page = self.request("GET", path, params=params)
            data = page.get("data", [])
            yield from data
            if not page.get("has_more", False):
                return
            skip += len(data)

    def get_me(self) -> dict:
        """GET /me/ — validate the API key and retrieve org info."""
        return self.request("GET", "/me/")


def _retry_delay(response: Any, attempt: int) -> float:
    """Seconds to wait: the Retry-After header, else exponential backoff."""
    headers = getattr(response, "headers", {}) or {}
    header = headers.get("Retry-After") or headers.get("retry-after")
    try:
        return float(header)
    except (TypeError, ValueError):
        return float(2**attempt)


def _is_transient(exc: Exception) -> bool:
    """True for network / timeout errors worth retrying."""
    try:
        import httpx

        return isinstance(exc, (httpx.TimeoutException, httpx.TransportError))
    except ImportError:  # pragma: no cover
        return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _content_hash(text: str) -> str:
    """Stable hash of rendered content for change detection."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _strip_html(html_text: str) -> str:
    """Strip HTML tags, keeping line breaks for block elements."""
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", html_text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def _truncate(text: str, limit: int) -> str:
    """Truncate text with a marker if it exceeds the limit."""
    if len(text) > limit:
        return text[:limit] + "\n... [truncated]"
    return text


# ---------------------------------------------------------------------------
# Renderers — one per object type
# ---------------------------------------------------------------------------


def _render_lead(lead: dict) -> str:
    """Render a lead to searchable plain text."""
    lines = [f"Lead: {lead.get('display_name', 'Unknown')}"]
    if lead.get("description"):
        lines.append(f"Description: {lead['description']}")
    if lead.get("url"):
        lines.append(f"Website: {lead['url']}")
    if lead.get("status_label"):
        lines.append(f"Status: {lead['status_label']}")
    for contact in lead.get("contacts", []):
        name = contact.get("display_name") or contact.get("name", "")
        title = contact.get("title", "")
        parts = [p for p in [name, title] if p]
        if parts:
            lines.append(f"Contact: {', '.join(parts)}")
        for email_entry in contact.get("emails", []):
            lines.append(f"  Email: {email_entry.get('email', '')}")
        for phone_entry in contact.get("phones", []):
            lines.append(f"  Phone: {phone_entry.get('phone', '')}")
    return "\n".join(lines)


def _render_opportunity(opp: dict, lead_name: str) -> str:
    """Render an opportunity to searchable plain text."""
    lines = [f"Opportunity: {opp.get('value_formatted', opp.get('value', ''))}"]
    lines.append(f"Lead: {lead_name}")
    if opp.get("status_label"):
        lines.append(f"Status: {opp['status_label']}")
    if opp.get("confidence"):
        lines.append(f"Confidence: {opp['confidence']}%")
    if opp.get("note"):
        lines.append(f"Note: {opp['note']}")
    if opp.get("date_won"):
        lines.append(f"Date won: {opp['date_won']}")
    return "\n".join(lines)


def _render_note(note: dict, lead_name: str) -> str:
    """Render a note to searchable plain text."""
    lines = [f"Note on lead: {lead_name}"]
    created = note.get("date_created", "")
    if created:
        lines.append(f"Date: {created[:10]}")
    if note.get("note_html"):
        lines.append(_strip_html(note["note_html"]))
    elif note.get("note"):
        lines.append(note["note"])
    return "\n".join(lines)


def _render_call(call: dict, lead_name: str) -> str:
    """Render a call to searchable plain text, including summary and transcript."""
    direction = call.get("direction", "unknown")
    disposition = call.get("disposition", "")
    duration = call.get("duration") or 0
    duration_str = f", {duration // 60} min" if duration else ""
    created = (call.get("date_created") or "unknown")[:10]

    lines = [
        f"Call ({direction}, {disposition or 'no disposition'}{duration_str}) on {created}"
    ]
    lines.append(f"Lead: {lead_name}")
    if call.get("summary"):
        lines.append(f"Summary: {call['summary']}")
    if call.get("recording_transcript"):
        lines.append(
            f"Transcript:\n{_truncate(call['recording_transcript'], _MAX_TRANSCRIPT_CHARS)}"
        )
    if call.get("voicemail_transcript"):
        lines.append(f"Voicemail: {call['voicemail_transcript']}")
    if call.get("note"):
        lines.append(f"Note: {call['note']}")
    return "\n".join(lines)


def _render_email(email_obj: dict, lead_name: str) -> str:
    """Render an email activity to searchable plain text."""
    lines = [f"Email: {email_obj.get('subject', '(no subject)')}"]
    lines.append(f"Lead: {lead_name}")
    if email_obj.get("direction"):
        lines.append(f"Direction: {email_obj['direction']}")
    if email_obj.get("sender"):
        lines.append(f"From: {email_obj['sender']}")
    to_field = email_obj.get("to", [])
    if to_field:
        to_str = ", ".join(to_field) if isinstance(to_field, list) else str(to_field)
        lines.append(f"To: {to_str}")
    created = email_obj.get("date_created", "")
    if created:
        lines.append(f"Date: {created[:10]}")
    if email_obj.get("body_text"):
        lines.append(f"\n{_truncate(email_obj['body_text'], _MAX_BODY_CHARS)}")
    elif email_obj.get("body_html"):
        lines.append(f"\n{_truncate(_strip_html(email_obj['body_html']), _MAX_BODY_CHARS)}")
    return "\n".join(lines)


_RENDERERS: dict[str, Callable] = {
    "lead": lambda obj, _lead_name: _render_lead(obj),
    "opportunity": _render_opportunity,
    "note": _render_note,
    "call": _render_call,
    "email": _render_email,
}


# ---------------------------------------------------------------------------
# Object → document row
# ---------------------------------------------------------------------------


def _lead_url(lead_id: str) -> str:
    """Close app URL for a lead."""
    return f"https://app.close.com/lead/{lead_id}/"


def _object_to_row(obj: dict, obj_type: str, lead_name: str) -> dict:
    """Convert a Close API object into a document row for dlt."""
    content = _RENDERERS[obj_type](obj, lead_name)
    obj_id = obj["id"]
    lead_id = obj.get("lead_id", obj_id)

    title_map = {
        "lead": f"Lead: {obj.get('display_name', 'Unknown')}",
        "opportunity": f"Opportunity — {lead_name}",
        "note": f"Note — {lead_name}",
        "call": f"Call — {lead_name}",
        "email": f"Email: {obj.get('subject', '(no subject)')} — {lead_name}",
    }

    return {
        "id": obj_id,
        "title": title_map.get(obj_type, obj_id),
        "content": content,
        "url": _lead_url(lead_id) if lead_id else "",
        "_deleted": False,
    }


# ---------------------------------------------------------------------------
# Lead name lookup
# ---------------------------------------------------------------------------


def _build_lead_lookup(client: _CloseClient) -> dict[str, str]:
    """Build {lead_id: display_name} for contextualizing activity documents."""
    lookup: dict[str, str] = {}
    for lead in client.iter_objects("/lead/", fields=["id", "display_name"]):
        lookup[lead["id"]] = lead.get("display_name", "Unknown")
    return lookup


# ---------------------------------------------------------------------------
# Sync state machine
# ---------------------------------------------------------------------------


def _iter_rows(
    client: _CloseClient,
    selected_types: tuple[str, ...],
    state: dict,
    stats: dict,
):
    """Yield document rows for new/changed objects and tombstones for deleted ones.

    All API calls complete before the first ``yield`` and state is written
    after the last one, so a failed run changes nothing.

    ``state`` is a plain dict standing in for dlt's resource state, so this
    function is directly unit-testable with a fake Close API and no dlt
    pipeline context.
    """
    stats.clear()
    stats.update(scanned=0, yielded=0, unchanged=0, deleted=0)

    previous: dict[str, dict] = dict(state.get("objects", {}))

    # Build lead name lookup if any activity types are selected.
    need_lead_lookup = bool(set(selected_types) & _ACTIVITY_TYPES)
    lead_names: dict[str, str] = _build_lead_lookup(client) if need_lead_lookup else {}

    # Single pass: fetch all objects, hash-compare, collect changes.
    # A failure anywhere aborts — no partial inventory drives tombstones.
    new_state: dict[str, dict] = {}
    rows: list[dict] = []

    for obj_type in selected_types:
        path = OBJECT_PATHS[obj_type]
        fields = _EXTRA_FIELDS.get(obj_type)
        for obj in client.iter_objects(path, fields=fields):
            obj_id = obj["id"]
            stats["scanned"] += 1

            # Resolve lead name for document context.
            if obj_type == "lead":
                lead_name = obj.get("display_name", "Unknown")
                # Populate the lookup in case it wasn't built separately.
                lead_names[obj_id] = lead_name
            else:
                lead_name = lead_names.get(obj.get("lead_id", ""), "Unknown")

            row = _object_to_row(obj, obj_type, lead_name)
            h = _content_hash(row["content"])
            new_state[obj_id] = {"type": obj_type, "hash": h}

            # Skip unchanged objects — stable content-hash means no re-ingestion.
            prev = previous.get(obj_id)
            if prev and prev.get("hash") == h:
                stats["unchanged"] += 1
                continue

            stats["yielded"] += 1
            rows.append(row)

    # Detect deletions: objects in previous state but absent from current listing.
    deletes: list[dict] = []
    for obj_id in sorted(set(previous) - set(new_state)):
        deletes.append({"id": obj_id, "_deleted": True})
        stats["deleted"] += 1

    # Yield deletes first (tombstones), then new/changed document rows.
    yield from deletes
    yield from rows

    # Commit new state only after all yields succeeded.
    state["objects"] = new_state
    logger.info("Close: sync stats %s", stats)


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def close_source(
    api_key: str | None = None,
    *,
    selected_types: list[str] | None = None,
    resource_name: str = "close_documents",
    check_active: Callable[[], None] | None = None,
    http_client: Any = None,
):
    """Return a ``dlt`` resource yielding Close CRM objects as documents.

    Hand the result to ``cognee.remember(...)`` with
    ``write_disposition="merge"`` and ``primary_key="id"``.

    Args:
        api_key: Close API key.  Falls back to ``CLOSE_API_KEY``.
        selected_types: Object types to ingest.  Default:
            ``["lead", "opportunity", "note", "call"]``.
            Choices: ``lead``, ``opportunity``, ``note``, ``call``, ``email``.
        resource_name: Stable dlt resource / table name.  Give each Close
            organization its own name when several sync into one dataset, so
            their incremental state stays separate.
        check_active: Optional host authorization checkpoint called periodically
            during extraction.
        http_client: Pre-built ``httpx.Client`` — mainly a test-injection point;
            when omitted one is built with a 30 s timeout.

    Returns:
        A ``dlt`` resource suitable for ``cognee.remember(...)`` /
        ``cognee.add(...)``.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError(
            "Close sync requires a Cognee build with table-scoped DLT document "
            "cleanup. Upgrade Cognee before syncing to avoid deleting another "
            "source's data."
        )

    resolved_key = api_key or os.environ.get("CLOSE_API_KEY")
    if not resolved_key:
        raise ValueError(
            "Close API key required: pass api_key= or set CLOSE_API_KEY."
        )

    types = _normalize_types(selected_types)
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        write_disposition="merge",
        primary_key="id",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def close_documents():
        import httpx as _httpx

        client = _CloseClient(
            http_client or _httpx.Client(timeout=30.0),
            resolved_key,
        )
        yield from dlt_utils.guarded_rows(
            _iter_rows(client, types, dlt.current.resource_state(), stats),
            check_active,
        )

    resource = close_documents()
    # Opt into the document ingestion path: each object becomes a text document
    # that flows through normal cognify (LLM graph extraction).
    setattr(resource, DOCUMENT_SOURCE_ATTR, CLOSE_SOURCE_NAME)
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    # Host-readable diagnostics (counts only, never titles or content).
    resource.cognee_sync_stats = stats
    return resource


def _normalize_types(selected_types: list[str] | None) -> tuple[str, ...]:
    """Validate and deduplicate the selected object types."""
    if selected_types is None:
        return DEFAULT_TYPES
    cleaned = tuple(dict.fromkeys(t.strip().lower() for t in selected_types if t.strip()))
    if not cleaned:
        raise ValueError(
            f"selected_types is empty. Choose from: {', '.join(ALL_TYPES)}."
        )
    invalid = set(cleaned) - set(ALL_TYPES)
    if invalid:
        raise ValueError(
            f"Unknown Close object type(s): {', '.join(sorted(invalid))}. "
            f"Choose from: {', '.join(ALL_TYPES)}."
        )
    return cleaned