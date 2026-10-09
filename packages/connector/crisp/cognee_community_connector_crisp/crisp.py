"""DLT source for Crisp conversations (full-snapshot sync + forget-on-delete).

Fetches Crisp shared-inbox conversations and their messages from the Crisp REST
API v1 (``https://api.crisp.chat/v1/``), renders each conversation to a single
markdown document, and yields it as a dlt resource for cognee's ingestion
pipeline.

Design notes
------------
* **One node per conversation.** Crisp sessions are short and numerous, so a
  conversation is aggregated into a *single* document (visitor context + the
  message transcript) rather than one row per message -- otherwise the graph
  explodes into thousands of near-identical nodes. This matches the issue's
  guidance and the Slack connector's channel-as-document model.

* **Document ingestion.** Like the Notion connector, the source declares
  ``cognee_document_source = "crisp"`` so ``resolve_dlt_sources`` tags each row
  ``external_metadata["source"] = "crisp"`` and it flows through the normal
  cognify entity-extraction pipeline (the right treatment for conversation
  prose), not the deterministic dlt-row schema path.

* **Full snapshot (``write_disposition="replace"``).** Each run rewrites staging
  with exactly the conversations currently visible to the integration. A
  conversation deleted or aged out upstream drops out of the listing, is absent
  from the snapshot, and cognee's ``orphan_cleanup`` forgets it from the graph +
  vector stores. Unchanged conversations keep a stable content-hash ``data_id``,
  so they are not re-ingested or re-cognified. Crisp has no delete feed, so the
  Slack-style full-snapshot model is the only way to see deletions.

* **Incremental cursor.** Crisp exposes ``updated_at`` on each conversation. We
  only *render* conversations whose ``updated_at`` is newer than a caller-supplied
  watermark (``since``), so a re-sync after the last run re-fetches just the
  changed conversations. Omitting ``since`` syncs everything.

* **Safety under ``replace``.** Because staging is authoritative, a *partial*
  snapshot would be reconciled downstream as a mass deletion. So a render/API
  error on any conversation aborts the run (leaving memory untouched) rather than
  committing a snapshot that forgets live conversations. Transient failures are
  retried first; only a persistent one reaches that abort.

Auth: Crisp's two-part token keypair (``identifier`` + ``key``) is sent as HTTP
Basic (base64 ``identifier:key``) with an ``X-Crisp-Tier: plugin`` header, plus
a ``website_id`` selecting the workspace. See https://docs.crisp.chat .
"""

from __future__ import annotations

import base64
import os
import time
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("crisp_connector")

# dlt resource / staging-table name for Crisp conversations.
CRISP_TABLE_NAME = "crisp_conversations"
CRISP_SOURCE_NAME = "crisp"

# Crisp REST API base + the tier header the plugin token routes expect.
_CRISP_BASE_URL = "https://api.crisp.chat/v1/"
_MAX_RETRIES = 5
# Crisp's per_page caps at 60; page-number pagination (not cursor).
_DEFAULT_PER_PAGE = 50
# Render only this many messages per conversation as a sanity guard.
_MAX_MESSAGES = 2000

_EXTRA_HINT = (
    "The Crisp connector needs an HTTP client: pip install httpx (used to call the Crisp REST API)."
)


def crisp_source(
    identifier: str | None = None,
    key: str | None = None,
    website_id: str | None = None,
    since: int | float | None = None,
    per_page: int = _DEFAULT_PER_PAGE,
    client: Any = None,
):
    """Create a dlt source that yields Crisp conversations as markdown documents.

    Args:
        identifier: Crisp token identifier. Falls back to ``CRISP_IDENTIFIER``.
        key: Crisp token key. Falls back to ``CRISP_KEY``.
        website_id: Crisp website (workspace) id. Falls back to ``CRISP_WEBSITE_ID``.
        since: Only conversations updated at/after this epoch-second watermark
            are rendered (incremental cursor). Omit to sync everything.
        per_page: Page size for conversation listing (max 60).
        client: Pre-built HTTP client exposing ``get(path, params)`` returning
            parsed JSON (a test-injection point). When omitted one is built from
            the credentials above.

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as import_exc:  # pragma: no cover - import guard
        raise ImportError(
            'The Crisp connector requires dlt: pip install "dlt[sqlalchemy]".'
        ) from import_exc

    if client is None:
        resolved_id = identifier or os.environ.get("CRISP_IDENTIFIER")
        resolved_key = key or os.environ.get("CRISP_KEY")
        resolved_site = website_id or os.environ.get("CRISP_WEBSITE_ID")
        if not (resolved_id and resolved_key and resolved_site):
            missing = [
                name
                for name, val in (
                    ("identifier", resolved_id),
                    ("key", resolved_key),
                    ("website_id", resolved_site),
                )
                if not val
            ]
            raise ValueError(
                "Crisp credentials required: pass identifier=/key=/website_id= or set "
                f"CRISP_IDENTIFIER/CRISP_KEY/CRISP_WEBSITE_ID (missing: {', '.join(missing)})."
            )
        client = _CrispClient(resolved_id, resolved_key, resolved_site)

    @dlt.resource(name=CRISP_TABLE_NAME, primary_key="session_id", write_disposition="replace")
    def crisp_conversations():
        # Full-snapshot sync: each run replaces staging with exactly the
        # conversations currently visible. A conversation that disappeared
        # upstream is absent here, so orphan_cleanup forgets it downstream.
        # An error on any conversation aborts the run (see module docstring):
        # a partial snapshot under replace would forget live conversations.
        count = 0
        for summary in _iter_conversations(client, per_page):
            session_id = summary.get("session_id")
            if not session_id:
                continue
            if since is not None and _updated_at(summary) < since:
                # Incremental cursor: skip conversations unchanged since the
                # watermark. They stay in prior state and are not re-cognified.
                continue
            count += 1
            yield _conversation_to_row(client, session_id, summary)
        logger.info("Crisp: synced %d conversation(s).", count)

    @dlt.source(name=CRISP_SOURCE_NAME)
    def _crisp():
        return crisp_conversations

    source = _crisp()
    # Opt into the document ingestion path (conversation -> text document ->
    # cognify). resolve_dlt_sources reads this marker; it never imports us.
    setattr(source, DOCUMENT_SOURCE_ATTR, CRISP_SOURCE_NAME)
    return source


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class _CrispClient:
    """Minimal Crisp REST API client (Basic auth + plugin tier header)."""

    def __init__(self, identifier: str, key: str, website_id: str):
        self._website_id = website_id
        # Crisp auth: base64("identifier:key") as HTTP Basic, plus the tier header.
        token = base64.b64encode(f"{identifier}:{key}".encode()).decode()
        self._headers = {
            "Authorization": f"Basic {token}",
            "X-Crisp-Tier": "plugin",
            "Content-Type": "application/json",
        }

    def get(self, path: str, params: dict | None = None):
        """GET a Crisp API path (relative to the base URL) and return parsed JSON.

        Retries rate-limit (429) / server (5xx) / timeout / network errors with
        backoff; a permanent error (401/403/404) propagates.
        """
        import httpx

        url = _CRISP_BASE_URL + path
        for attempt in range(_MAX_RETRIES):
            try:
                resp = httpx.get(url, headers=self._headers, params=params, timeout=30)
            except httpx.TransportError:
                if attempt == _MAX_RETRIES - 1:
                    raise
                _sleep_backoff(attempt)
                continue

            if resp.status_code in (429, 500, 502, 503, 504):
                if attempt == _MAX_RETRIES - 1:
                    resp.raise_for_status()
                _sleep_backoff(attempt, resp.headers.get("Retry-After"))
                continue

            resp.raise_for_status()
            return resp.json()

        raise RuntimeError("Crisp: exhausted retries")  # pragma: no cover


def _sleep_backoff(attempt: int, retry_after: str | None = None) -> None:
    if retry_after:
        try:
            time.sleep(max(0.0, float(retry_after)))
            return
        except (TypeError, ValueError):
            pass
    time.sleep(float(2**attempt))


# ---------------------------------------------------------------------------
# Crisp API helpers (module-private, unit-tested)
# ---------------------------------------------------------------------------


def _iter_conversations(client, per_page: int):
    """Yield conversation summary objects across Crisp's page-number pagination.

    Crisp lists conversations at ``/conversations/{page_number}`` with a page
    number (not a cursor). We stop on a short/empty page or a hard page cap so a
    contract violation can't loop forever.
    """
    page = 1
    seen = 0
    # Generous cap: 10k conversations at the default page size.
    max_pages = max(1, 10000 // max(1, per_page))
    site = getattr(client, "website_id", None) or getattr(client, "_website_id", None)
    while page <= max_pages:
        data = client.get(
            f"website/{site}/conversations/{page}",
            params={"per_page": per_page},
        )
        items = (data or {}).get("data") or []
        if not items:
            return
        yield from items
        seen += len(items)
        if len(items) < per_page:
            return
        page += 1


def _updated_at(conversation: dict) -> float:
    """Extract the ``updated_at`` epoch-seconds from a conversation summary."""
    return float(conversation.get("updated_at") or 0)


def _conversation_to_row(client, session_id: str, summary: dict) -> dict:
    """Flatten a Crisp conversation (+ its messages) into one document row.

    Only identity/provenance + the rendered transcript are kept -- no volatile
    ``updated_at`` in the row -- so a metadata-only bump does not churn the
    content-hash ``data_id`` (matching the Notion connector's discipline).
    """
    messages = _list_messages(client, session_id)
    return {
        "session_id": session_id,
        "url": summary.get("url"),
        "title": _conversation_title(summary),
        "content": _render_conversation(summary, messages),
    }


def _list_messages(client, session_id: str) -> list[dict]:
    """Fetch messages for a conversation, paginating via ``timestamp_before``.

    Crisp returns messages newest-first and pages backwards with a
    ``timestamp_before`` cursor. We collect them, cap at ``_MAX_MESSAGES``, and
    return oldest-first for a natural transcript order.
    """
    collected: list[dict] = []
    before: int | None = None
    site = getattr(client, "website_id", None) or getattr(client, "_website_id", None)
    while len(collected) < _MAX_MESSAGES:
        params = {"timestamp_before": before} if before is not None else None
        data = client.get(
            f"website/{site}/conversation/{session_id}/messages",
            params=params,
        )
        items = (data or {}).get("data") or []
        if not items:
            break
        collected.extend(items)
        # The oldest message in this page is the cursor for the next page back.
        oldest = min(_message_ts(m) for m in items)
        if before is not None and oldest >= before:
            break  # no forward progress: stop rather than loop
        before = oldest
    # Oldest-first for a readable transcript.
    return list(reversed(collected[:_MAX_MESSAGES]))


def _message_ts(message: dict) -> int:
    return int(message.get("timestamp") or 0)


def _conversation_title(conversation: dict) -> str:
    """Human-readable conversation title: subject, else visitor, else id."""
    meta = conversation.get("meta") or {}
    subject = (meta.get("subject") or "").strip()
    if subject:
        return subject
    nickname = (meta.get("nickname") or "").strip()
    if nickname:
        return f"Conversation with {nickname}"
    return f"Conversation {conversation.get('session_id', '')}".strip()


def _render_conversation(summary: dict, messages: list[dict]) -> str:
    """Render a conversation (visitor context + transcript) to markdown."""
    lines: list[str] = []
    meta = summary.get("meta") or {}
    nickname = meta.get("nickname")
    origin = meta.get("origin")
    if nickname or origin:
        ctx = " · ".join(x for x in [nickname, origin] if x)
        lines.append(f"_Visitor: {ctx}_")
        lines.append("")
    for message in messages:
        rendered = _render_message(message)
        if rendered:
            lines.append(rendered)
    return "\n\n".join(lines)


def _render_message(message: dict) -> str:
    """Render a single Crisp message to one markdown line.

    Only text/note messages carry human content; file/audio/picker/etc. are
    summarized so the transcript stays readable. Compose-preview and other
    non-content events are skipped.
    """
    mtype = message.get("type")
    if mtype not in ("text", "note"):
        return ""
    who = message.get("from")  # "user" (visitor) or "operator"
    prefix = "Visitor" if who == "user" else "Agent"
    content = _message_text(message)
    if not content:
        return ""
    return f"**{prefix}:** {content}"


def _message_text(message: dict) -> str:
    """Extract displayable text from a Crisp message.

    A text/note message carries ``content`` as a plain string. Some message
    types nest text under ``content``; normalize both to a string.
    """
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
        # e.g. {"text": "..."} for some rich types.
        return str(content.get("text") or content.get("value") or "").strip()
    return ""
