"""DLT source for Guru cards (full-snapshot sync + forget-on-delete).

Fetches cards from a Guru workspace and renders their HTML or Markdown bodies
to text, then yields them as a dlt resource for cognee's ingestion pipeline.

Like Notion, Guru cards are *documents*: the source declares
``cognee_document_source = "guru"``, so ``resolve_dlt_sources`` tags each row
``external_metadata["source"] = "guru"`` (not ``"dlt"``) and every card flows
through the standard cognify entity-extraction pipeline rather than the
deterministic dlt-row schema path.

The source is a full snapshot: ``write_disposition="replace"`` rewrites staging
with exactly the cards the token can see on each run. Deletions propagate for
free -- an archived or deleted card drops out of Guru's search results, so it is
absent from the snapshot and cognee's ``orphan_cleanup`` removes it from the
graph and vector stores. Unchanged cards keep a stable content-hash ``data_id``,
so a re-sync only re-cognifies what actually changed.

Scoping is server-side via Guru Query Language (``q``) -- by folder, by
verification state, or both -- so a scoped sync still sees the complete set of
cards in that scope and therefore still forgets deletions correctly. A
last-modified filter is deliberately *not* offered here: under ``replace`` a
partial snapshot would look like mass deletion upstream.
"""

import os
import re
import time
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlsplit

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
_HTML_HINT = re.compile(r"<(p|div|br|li|h[1-6]|table|ul|ol)\b", re.IGNORECASE)


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
        client: Pre-built client exposing ``get_cards(**params)`` and returning
            ``(cards, next_token)`` (mainly a test-injection point); when omitted
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

    params = _search_params(folder_id, verification_state)

    @dlt.resource(name=GURU_TABLE_NAME, primary_key="id", write_disposition="replace")
    def guru_cards():
        # Full-snapshot sync: each run replaces staging with exactly the cards
        # currently visible to the token. Archived/deleted cards drop out of
        # Guru's search results, so they fall out of staging and cognee's
        # orphan_cleanup then forgets them from the graph + vector stores.
        #
        # A fetch error is NOT swallowed: staging is authoritative under
        # replace, so a partial snapshot would forget live cards as if they were
        # deleted upstream. Letting the error abort leaves staging -- and memory
        # -- untouched, which is the safe failure. Transient blips are retried in
        # _request; only a persistent failure reaches here.
        count = 0
        for card in _paginate(client, **params):
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
    # Opt into the document ingestion path (card -> text document -> cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(source, DOCUMENT_SOURCE_ATTR, GURU_SOURCE_NAME)
    return source


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class _GuruClient:
    """Minimal HTTP client for the Guru v1 API.

    Wraps the two things the raw API leaves to the caller: HTTP basic auth and
    ``Link``-header pagination (``rel="next-page"``).
    """

    def __init__(self, email: str, token: str, timeout: float = 30.0):
        import httpx

        self._httpx = httpx
        self._client = httpx.Client(auth=(email, token), base_url=_GURU_API_ROOT, timeout=timeout)

    def get_cards(self, **params) -> tuple[list[dict], str | None]:
        """Return one page of cards plus the next-page token, if any."""
        response = _request(self._client.get, _CARDS_PATH, params=params)
        return list(response.json()), _next_page_token(response)

    def close(self) -> None:
        self._client.close()


def _build_query(folder_id: str | None, verification_state: str | None) -> str:
    """Compose a Guru Query Language filter for the requested scope.

    Folders keep Guru's legacy "board" naming: ``boards`` is the documented
    field name for the folders a card sits in.
    """
    clauses = []
    if folder_id:
        clauses.append(f'boards CONTAINS ("{folder_id}")')
    if verification_state:
        clauses.append(f"verificationState = {verification_state}")
    return " AND ".join(clauses)


def _search_params(folder_id: str | None, verification_state: str | None) -> dict[str, Any]:
    """Build the ``/search/query`` parameters for the requested scope.

    Sorting by ``lastModified`` keeps each run's snapshot in a stable order, so
    re-syncing an unchanged workspace produces an identical load.
    """
    params: dict[str, Any] = {
        "maxResults": _PAGE_SIZE,
        "showArchived": False,
        "sortField": "lastModified",
        "sortOrder": "DESC",
    }
    query = _build_query(folder_id, verification_state)
    if query:
        params["q"] = query
    return params


def _request(method, *args, **kwargs):
    """Call the Guru API, retrying rate-limit / transient responses.

    httpx does not retry for us and Guru throttles per workspace, so a large
    workspace would otherwise 429 and abort the sync. Rate-limit (429), server
    (5xx), timeout and network errors are retried with backoff, honouring
    ``Retry-After``; permanent errors (auth, not-found) and exhausted retries
    propagate so the caller can decide.
    """
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


def _next_page_token(response) -> str | None:
    """Extract the paging ``token`` from a ``Link: <...>; rel="next-page"`` header."""
    headers = getattr(response, "headers", None) or {}
    link = headers.get("link") or headers.get("Link")
    if not link or 'rel="next-page"' not in link:
        return None
    target = re.search(r"<([^>]+)>", link)
    if not target:
        return None
    token = parse_qs(urlsplit(target.group(1)).query).get("token")
    return token[0] if token else None


# ---------------------------------------------------------------------------
# Card iteration / mapping
# ---------------------------------------------------------------------------


def _paginate(client, **params):
    """Yield every card, following Guru's paging tokens.

    Guarded against a repeated token: the API contract says the header is absent
    on the last page, so a token we have already followed means the response is
    malformed and continuing would loop forever.
    """
    token = None
    seen: set[str] = set()
    while True:
        page = dict(params)
        if token:
            page["token"] = token
        cards, next_token = client.get_cards(**page)
        yield from cards
        if not next_token or next_token in seen:
            return
        seen.add(next_token)
        token = next_token


def _card_to_row(card: dict) -> dict:
    """Flatten a Guru card into a document row.

    Only ``title``/``content`` (+ ``id``/``url`` for identity and provenance)
    are kept, so a metadata-only edit that bumps ``lastModified`` without
    changing the text does not churn the content-hash ``data_id``.
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
    return f"https://app.getguru.com/cards/{slug}" if slug else None


def _card_text(card: dict) -> str:
    """Card body as text, prefixed with the trust and placement context.

    Verification state is carried into the graph on purpose: an unverified card
    is stale knowledge, and the only way that signal reaches cognify is as part
    of the card's text.
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
    """Flatten a card body to text.

    Guru stores card bodies as HTML or Markdown, so only bodies that actually
    look like HTML are parsed; anything else is already text and passes through.
    """
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

    def handle_data(self, data):
        if not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)
