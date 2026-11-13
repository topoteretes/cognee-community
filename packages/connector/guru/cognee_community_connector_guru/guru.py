"""DLT source for Guru cards (full-snapshot sync + forget-on-delete).

Cards are ingested as documents (``DOCUMENT_SOURCE_ATTR = "guru"``), so they go
through cognify like Notion pages. Each run is a full snapshot under
``write_disposition="replace"``: archived or deleted cards drop out of Guru's
search results and cognee's ``orphan_cleanup`` forgets them. Unchanged cards
keep the same content hash, so only edited cards are re-cognified.

Scoping (folder, verification state) is done server-side with Guru Query
Language, so a scoped sync still sees every card in its scope. There is no
``lastModified`` filter: under ``replace`` a partial snapshot reads as deletion.
"""

import os
import re
import time
from html.parser import HTMLParser
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("guru_connector")

# dlt resource / staging-table name for Guru cards.
GURU_TABLE_NAME = "guru_cards"
GURU_SOURCE_NAME = "guru"

_GURU_API_ROOT = "https://api.getguru.com/api/v1"
_CARDS_PATH = "/search/query"
# Guru caps ``maxResults`` at 50.
_PAGE_SIZE = 50
_MAX_RETRIES = 5

_EXTRA_HINT = 'The Guru connector requires httpx: pip install "httpx>=0.27,<1"'

# Tags that imply a line break when flattening card HTML to text.
_BLOCK_TAGS = frozenset(
    {
        "article",
        "blockquote",
        "br",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "p",
        "pre",
        "section",
        "tr",
    }
)
# Never surface script/style bodies as card content.
_SKIPPED_TAGS = frozenset({"script", "style"})
# Presence of any of these means the body is HTML rather than Markdown.
_HTML_HINT = re.compile(
    r"<(p|div|br|li|h[1-6]|table|ul|ol|span|a|strong|em|b|i|img|pre|code)\b", re.IGNORECASE
)
# Table cells are inline in HTML; separate them so adjacent cells don't fuse.
_CELL_TAGS = frozenset({"td", "th"})
# Guru Query Language values for ``verificationState``.
_VERIFICATION_STATES = frozenset({"trusted", "needsVerification"})


def guru_source(
    email: str | None = None,
    token: str | None = None,
    *,
    folder_id: str | None = None,
    verification_state: str | None = None,
    client: Any = None,
):
    """Create a dlt source that yields Guru cards as text documents.

    Args:
        email: Guru account email. Falls back to ``GURU_USER``.
        token: Guru API token. Falls back to ``GURU_TOKEN``.
        folder_id: Restrict ingestion to cards in this folder (Guru's legacy
            "board"). Omit to ingest every card the token can see.
        verification_state: Restrict to ``"trusted"`` or ``"needsVerification"``.
        client: Pre-built client exposing ``get_cards(url, params)`` and returning
            ``(cards, next_url)`` (mainly a test-injection point); when omitted
            an HTTP client is built from the credentials above.

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    if client is None:
        resolved_email = email or os.environ.get("GURU_USER")
        resolved_token = token or os.environ.get("GURU_TOKEN")
        if not resolved_email or not resolved_token:
            raise ValueError(
                "Guru credentials required: pass email= and token=, or set GURU_USER and "
                "GURU_TOKEN."
            )
        client = _GuruClient(resolved_email, resolved_token)

    if verification_state is not None and verification_state not in _VERIFICATION_STATES:
        raise ValueError(
            f"verification_state must be one of {sorted(_VERIFICATION_STATES)}, "
            f"got {verification_state!r}"
        )
    if folder_id is not None and '"' in folder_id:
        raise ValueError(f"Invalid Guru folder id: {folder_id!r}")
    params = _search_params(folder_id, verification_state)

    @dlt.resource(name=GURU_TABLE_NAME, primary_key="id", write_disposition="replace")
    def guru_cards():
        # Fetch errors are not swallowed: under replace, a partial snapshot
        # would forget live cards. Aborting leaves memory untouched.
        count = 0
        for card in _paginate(client, params):
            row = _card_to_row(card)
            if row["id"] is None:
                logger.warning("Guru: skipping card without an id: %r", card.get("preferredPhrase"))
                continue
            count += 1
            yield row
        logger.info("Guru: synced %d card(s).", count)

    @dlt.source(name=GURU_SOURCE_NAME)
    def _guru():
        return guru_cards

    source = _guru()
    # Route cards through cognify as documents, not dlt rows.
    setattr(source, DOCUMENT_SOURCE_ATTR, GURU_SOURCE_NAME)
    return source


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class _GuruClient:
    """Minimal Guru v1 API client: basic auth + ``Link``-header paging."""

    def __init__(self, email: str, token: str, timeout: float = 30.0):
        import httpx

        self._client = httpx.Client(auth=(email, token), base_url=_GURU_API_ROOT, timeout=timeout)

    def get_cards(self, url: str, params: dict | None = None) -> tuple[list[dict], str | None]:
        """Return one page of cards plus the next page's URL, if any."""
        response = _request(self._get, url, params=params)
        cards = response.json()
        # Never read an unexpected body as "no cards": that would forget everything.
        if not isinstance(cards, list):
            raise ValueError(f"Guru: expected a list of cards, got {type(cards).__name__}")
        return cards, response.links.get("next-page", {}).get("url")

    def _get(self, url: str, **kwargs):
        # httpx does not raise on 4xx/5xx; raise so _request can retry or abort.
        response = self._client.get(url, **kwargs)
        response.raise_for_status()
        return response

    def close(self) -> None:
        self._client.close()


def _build_query(folder_id: str | None, verification_state: str | None) -> str:
    """Build a Guru Query Language filter. Folders are still called ``boards``."""
    clauses = []
    if folder_id:
        clauses.append(f'boards CONTAINS ("{folder_id}")')
    if verification_state:
        clauses.append(f"verificationState = {verification_state}")
    return " AND ".join(clauses)


def _search_params(folder_id: str | None, verification_state: str | None) -> dict[str, Any]:
    """Build ``/search/query`` params.

    Oldest-first by creation date, so edits during a sync can't reorder pages.
    """
    params: dict[str, Any] = {
        "maxResults": _PAGE_SIZE,
        "showArchived": False,
        "sortField": "dateCreated",
        "sortOrder": "ASC",
    }
    query = _build_query(folder_id, verification_state)
    if query:
        params["q"] = query
    return params


def _request(method, *args, **kwargs):
    """Call the Guru API, retrying 429/5xx/network errors (honours ``Retry-After``)."""
    for attempt in range(_MAX_RETRIES):
        try:
            return method(*args, **kwargs)
        except Exception as exc:
            if attempt == _MAX_RETRIES - 1 or not _is_transient(exc):
                raise
            delay = _retry_after(getattr(exc, "response", None), attempt)
            logger.warning(
                "Guru: %s — retrying in %.1fs (%d/%d).", exc, delay, attempt + 1, _MAX_RETRIES
            )
            time.sleep(delay)


def _is_transient(exc: Exception) -> bool:
    """True for rate-limit / server / timeout / network errors worth retrying."""
    import httpx

    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status == 429 or (status is not None and 500 <= status < 600)


def _retry_after(response, attempt: int) -> float:
    """Seconds to wait before retrying: the Retry-After header, else backoff."""
    headers = getattr(response, "headers", None) or {}
    header = headers.get("retry-after") or headers.get("Retry-After")
    try:
        return float(header)
    except (TypeError, ValueError):
        return float(2**attempt)


# ---------------------------------------------------------------------------
# Card iteration / mapping
# ---------------------------------------------------------------------------


def _paginate(client, params: dict[str, Any]):
    """Yield every card, following ``next-page`` URLs as-is; stop on a repeat."""
    url, page_params = _CARDS_PATH, params
    seen: set[str] = set()
    while True:
        cards, next_url = client.get_cards(url, page_params)
        yield from cards
        if not next_url or next_url in seen:
            return
        seen.add(next_url)
        url, page_params = next_url, None


def _card_to_row(card: dict) -> dict:
    """Flatten a card into a document row.

    No volatile fields (e.g. ``lastModified``), so the content hash only changes
    when the text does.
    """
    return {
        "id": card.get("id"),
        "url": _card_url(card),
        "title": (card.get("preferredPhrase") or "").strip(),
        "content": _card_text(card),
    }


def _card_url(card: dict) -> str | None:
    """The card's web URL, built from its slug."""
    slug = card.get("slug")
    return f"https://app.getguru.com/card/{slug}" if slug else None


def _card_text(card: dict) -> str:
    """Card body as text, prefixed with verification state and placement.

    Verification state goes in the text so cognify can tell stale cards apart.
    """
    header = [line for line in (_trust_line(card), _placement_line(card)) if line]
    body = _html_to_text(card.get("content") or "")
    return "\n".join(header + ([body] if body else []))


def _trust_line(card: dict) -> str:
    """A one-line summary of the card's verification state."""
    state = card.get("verificationState")
    last_verified = card.get("lastVerified")
    if not state and not last_verified:
        return ""
    line = f"Verification: {state}" if state else "Verification"
    if state == "NEEDS_VERIFICATION" and card.get("verificationReason"):
        line += f" ({card['verificationReason']})"
    if last_verified:
        line += f" — last verified {last_verified}"
    return line


def _placement_line(card: dict) -> str:
    """A one-line summary of where the card lives (collection and folders)."""
    parts = []
    collection = (card.get("collection") or {}).get("name")
    if collection:
        parts.append(f"Collection: {collection}")
    boards = [board.get("title") for board in card.get("boards") or [] if board.get("title")]
    if boards:
        parts.append("Folders: " + ", ".join(boards))
    return " | ".join(parts)


def _html_to_text(content: str) -> str:
    """Flatten an HTML card body to text; Markdown passes through."""
    if not _HTML_HINT.search(content):
        return _tidy(content)

    extractor = _TextExtractor()
    extractor.feed(content)
    extractor.close()
    return _tidy(extractor.text())


def _tidy(text: str) -> str:
    """Collapse runs of blank lines and strip trailing spaces per line."""
    lines = [line.strip() for line in text.splitlines()]
    out: list[str] = []
    for line in lines:
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    return "\n".join(out).strip()


class _TextExtractor(HTMLParser):
    """Collect the readable text of a card body, keeping block boundaries."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIPPED_TAGS:
            self._skip_depth += 1
        elif not self._skip_depth and tag == "br":
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIPPED_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif not self._skip_depth and tag in _BLOCK_TAGS and tag != "br":
            self._parts.append("\n")
        elif not self._skip_depth and tag in _CELL_TAGS:
            self._parts.append(" ")

    def handle_data(self, data):
        if not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)
