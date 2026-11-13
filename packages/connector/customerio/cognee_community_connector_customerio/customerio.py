"""DLT source for Customer.io messages (full-snapshot sync + forget-on-delete).

Reads the messages inside Customer.io campaigns (shown as "Automations" in the
UI) and newsletters (shown as "One-time sends") through the App API, and yields
one text document per message for cognee to cognify.

Customer.io has no change feed, and a campaign's ``updated`` does not move when
one of its messages is edited, so every run has to list every message anyway.
The source is therefore a full snapshot, like the Notion connector: cognee's
default ``write_disposition="replace"`` rewrites staging with exactly the
messages that exist now, and ``orphan_cleanup`` forgets the ones that were
deleted. Rows carry only the message text (no ``updated``/``deduplicate_id``),
so a message whose text did not change keeps its content-hash ``data_id`` and
is not re-cognified, even though Customer.io bumps ``updated`` on every
language version whenever one of them is published.
"""

import os
import re
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

import dlt
import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("customerio_connector")

CUSTOMERIO_SOURCE_NAME = "customerio"
MESSAGES_TABLE_NAME = "customerio_messages"

_BASE_URLS = {"us": "https://api.customer.io/v1", "eu": "https://api-eu.customer.io/v1"}

# Message types ingested by default. Others the API knows about ("whatsapp",
# "slack", "inbox", "live_notification") can be opted into with
# ``message_types=``.
DEFAULT_MESSAGE_TYPES = ("email", "push", "in_app", "twilio")
# Webhook bodies are request payloads that often carry URLs and tokens, not
# prose, so they are never ingested.
_NEVER_INGESTED = frozenset({"webhook"})
# Types whose ``body`` is an HTML document rather than plain text.
_HTML_TYPES = frozenset({"email", "in_app", "inbox"})
# How a type is named in the document text.
_CHANNEL_NAMES = {"twilio": "sms", "in_app": "in-app"}

# The App API allows 10 requests per second.
_MIN_INTERVAL_SECONDS = 0.1
# Retry budget for rate-limited / transient responses. Kept short because a
# broken message 500s on every attempt (see ``_list_messages``).
_MAX_RETRIES = 4
_TRANSIENT_STATUSES = (429, 500, 502, 503, 504)
_TIMEOUT_SECONDS = 30


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


def _make_session(api_key: str) -> requests.Session:
    """Build a session authenticated with an App API key (Bearer token)."""
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {api_key}"
    session.headers["Accept"] = "application/json"
    return session


class _Api:
    """GETs against the App API, paced to 10 req/s and retried on transient errors."""

    def __init__(self, session: Any, base_url: str):
        self.session = session
        self.base_url = base_url
        self._next_request_at = 0.0

    def get(self, path: str, params: dict | None = None) -> requests.Response:
        """GET ``path`` and return the response as-is, even a 404 or a final 500.

        Only exhausted retries on a network error raise here; callers decide
        what a status means.
        """
        url = f"{self.base_url}{path}"
        for attempt in range(_MAX_RETRIES):
            last_attempt = attempt == _MAX_RETRIES - 1
            self._pace()
            try:
                response = self.session.get(url, params=params, timeout=_TIMEOUT_SECONDS)
            except (requests.ConnectionError, requests.Timeout) as exc:
                if last_attempt:
                    raise
                delay, reason = float(2**attempt), str(exc)
            else:
                if response.status_code not in _TRANSIENT_STATUSES or last_attempt:
                    return response
                delay = _retry_after(response.headers, attempt)
                reason = f"HTTP {response.status_code}"
            logger.warning(
                "Customer.io: %s on %s — retrying in %.1fs (%d/%d).",
                reason,
                path,
                delay,
                attempt + 1,
                _MAX_RETRIES,
            )
            time.sleep(delay)
        raise RuntimeError("unreachable")  # the loop always returns or raises

    def get_json(self, path: str, params: dict | None = None) -> dict:
        response = self.get(path, params)
        response.raise_for_status()
        return response.json()

    def _pace(self) -> None:
        wait = self._next_request_at - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._next_request_at = time.monotonic() + _MIN_INTERVAL_SECONDS


def _retry_after(headers, attempt: int) -> float:
    """Seconds to wait before retrying: the Retry-After header, else backoff."""
    try:
        return float((headers or {}).get("Retry-After"))
    except (TypeError, ValueError):
        return float(2**attempt)


def _paginate(api: _Api, path: str, key: str) -> Iterator[dict]:
    """Yield every item of a list endpoint, following ``next`` cursors.

    A listing that can't be completed raises, so a partial list can never
    reach a full-snapshot load and be mistaken for deletions.
    """
    params: dict = {}
    seen_cursors: set[str] = set()
    while True:
        body = api.get_json(path, params)
        yield from body.get(key) or []
        cursor = body.get("next")
        if not cursor:
            return
        if cursor in seen_cursors:
            raise RuntimeError(f"Customer.io: {path} returned the same page cursor twice.")
        seen_cursors.add(cursor)
        params = {"start": cursor}


# ---------------------------------------------------------------------------
# Listing messages
# ---------------------------------------------------------------------------


@dataclass
class _RunStats:
    """Counts message fetches so a run where nothing could be read aborts."""

    fetched: int = 0
    skipped: int = 0


def _list_messages(
    api: _Api, list_path: str, key: str, item_key: str, ids: Sequence[Any], stats: _RunStats
) -> list[dict]:
    """List the messages under one campaign or newsletter.

    Uses the list endpoint first. In a live workspace, one half-built message
    (an SMS step with no provider) made ``/campaigns/{id}/actions`` return 500
    for the whole campaign while every other action still loaded on its own.
    So when the list fails with a server error, each message is fetched by id
    from ``ids`` (the parent object lists them), and a message that keeps
    failing is skipped with a warning instead of blocking the whole sync.
    """
    try:
        messages = list(_paginate(api, list_path, key))
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status == 404:
            return []  # the parent was deleted after it was listed
        if status is None or status < 500:
            raise
        logger.warning(
            "Customer.io: %s failed (%s); fetching its %d message(s) one by one.",
            list_path,
            exc,
            len(ids),
        )
    else:
        stats.fetched += len(messages)
        return messages

    messages = []
    for message_id in ids:
        response = api.get(f"{list_path}/{message_id}")
        if response.status_code == 404:
            continue  # deleted since the parent was listed
        if response.status_code >= 500:
            stats.skipped += 1
            logger.warning(
                "Customer.io: %s/%s keeps failing (HTTP %d), skipping it.",
                list_path,
                message_id,
                response.status_code,
            )
            continue
        response.raise_for_status()
        messages.append(response.json()[item_key])
        stats.fetched += 1
    return messages


def sync_messages(
    api: _Api,
    message_types: Sequence[str] = DEFAULT_MESSAGE_TYPES,
    include_campaigns: bool = True,
    include_newsletters: bool = True,
) -> Iterator[dict[str, Any]]:
    """Yield a document row for every message currently in the workspace."""
    stats = _RunStats()
    yielded = 0

    if include_campaigns:
        for campaign in _paginate(api, "/campaigns", "campaigns"):
            ids = [action["id"] for action in campaign.get("actions") or []]
            actions = _list_messages(
                api, f"/campaigns/{campaign['id']}/actions", "actions", "action", ids, stats
            )
            for action in actions:
                row = _message_to_row("action", action, campaign.get("name"), message_types)
                if row:
                    yielded += 1
                    yield row

    if include_newsletters:
        for newsletter in _paginate(api, "/newsletters", "newsletters"):
            if newsletter.get("type") not in message_types:
                continue
            contents = _list_messages(
                api,
                f"/newsletters/{newsletter['id']}/contents",
                "contents",
                "content",
                newsletter.get("content_ids") or [],
                stats,
            )
            for content in contents:
                row = _message_to_row(
                    "newsletter_content", content, newsletter.get("name"), message_types
                )
                if row:
                    yielded += 1
                    yield row

    # Skipping one broken message is fine; skipping all of them means the API
    # (not one message) is failing, and a load now would forget everything.
    if stats.skipped and not stats.fetched:
        raise RuntimeError(
            f"Customer.io: none of {stats.skipped} message request(s) succeeded; "
            "aborting so nothing is forgotten."
        )
    logger.info(
        "Customer.io: synced %d message(s), skipped %d that failed to load.",
        yielded,
        stats.skipped,
    )


# ---------------------------------------------------------------------------
# Messages → document rows
# ---------------------------------------------------------------------------


def _message_to_row(
    kind: str, message: dict, parent_name: str | None, message_types: Sequence[str]
) -> dict[str, Any] | None:
    """Render a message as a document row ``{id, title, content}``, or ``None`` to skip it.

    ``kind`` prefixes the id because campaign actions and newsletter contents
    are numbered separately (both had id 5 in a test workspace). Only the
    message text goes into the row: no addresses, headers or Liquid
    recipients, and no timestamps, so an unchanged message keeps its content
    hash. Messages with no text at all (an unfinished push step) are skipped.
    """
    message_type = message.get("type")
    if message_type not in message_types:
        return None

    body = message.get("body") or message.get("body_plain") or ""
    text = _html_to_text(body) if message_type in _HTML_TYPES else _clean_text(body)
    subject = _clean_text(message.get("subject"))
    if not text and not subject:
        return None

    name = message.get("name") or ""
    parent = parent_name or ""
    # The "(fr)" / "(en)" translation names already start with the parent's
    # name ("Welcome: Email 1 (fr)"), so don't repeat it.
    title = name if not parent or name.startswith(parent) else f"{parent}: {name}"

    lines = [
        f"{'Campaign' if kind == 'action' else 'Newsletter'}: {parent}",
        f"Message: {name}",
        f"Channel: {_CHANNEL_NAMES.get(message_type, message_type)}",
        # Customer.io uses "" for the default language.
        f"Language: {message.get('language') or 'default'}",
    ]
    if subject:
        lines.append(f"{'Title' if message_type == 'push' else 'Subject'}: {subject}")
    preheader = _clean_text(message.get("preheader_text"))
    if preheader:
        lines.append(f"Preheader: {preheader}")

    return {
        "id": f"{kind}:{message['id']}",
        "title": title,
        "content": "\n".join(lines) + (f"\n\n{text}" if text else ""),
    }


# Liquid control tags ({% if %}, {% unsubscribe_url %}) are logic, not text.
# Output tags like {{customer.first_name}} are kept: they read as placeholders.
_LIQUID_TAG = re.compile(r"\{%.*?%\}", re.DOTALL)


def _clean_text(value: Any) -> str:
    """Strip Liquid control tags and blank lines from a plain-text field."""
    if not value:
        return ""
    lines = (" ".join(line.split()) for line in _LIQUID_TAG.sub("", str(value)).splitlines())
    return "\n".join(line for line in lines if line)


class _TextExtractor(HTMLParser):
    """Collect the visible text of an HTML message, one block per line."""

    _SKIPPED = frozenset({"head", "style", "script", "title"})
    _BLOCKS = frozenset(
        {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section"}
    )

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0
        self._link_stack: list[bool] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIPPED:
            self._skip_depth += 1
        elif tag == "a":
            href = (dict(attrs).get("href") or "").lower()
            # Customer.io's footer link ("Unsubscribe", "Se désabonner") would
            # otherwise end every message in the graph.
            self._link_stack.append("unsubscribe" in href or "subscription_preferences" in href)
        if tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIPPED and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "a" and self._link_stack:
            self._link_stack.pop()
        if tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip_depth and not any(self._link_stack):
            self.parts.append(data)


def _html_to_text(html: str) -> str:
    """Turn an HTML message body into plain text without the unsubscribe footer."""
    if not html:
        return ""
    extractor = _TextExtractor()
    extractor.feed(html)
    extractor.close()
    return _clean_text("".join(extractor.parts))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def customerio_source(
    api_key: str | None = None,
    region: str | None = None,
    message_types: Sequence[str] = DEFAULT_MESSAGE_TYPES,
    include_campaigns: bool = True,
    include_newsletters: bool = True,
    session: Any = None,
):
    """Create a dlt source that syncs Customer.io message content into cognee.

    Args:
        api_key: An App API key (Account settings → API Credentials → App API
            Keys; not a Track API key). Falls back to ``CUSTOMERIO_APP_API_KEY``.
        region: ``"us"`` or ``"eu"``, matching the region chosen when the
            workspace was created. Falls back to ``CUSTOMERIO_REGION``, then "us".
        message_types: Message types to ingest. Defaults to email, push,
            in-app and SMS (``"twilio"``). Webhooks are never ingested.
        include_campaigns: Sync messages in campaigns ("Automations").
        include_newsletters: Sync messages in newsletters ("One-time sends").
        session: Pre-built ``requests`` session. Mainly an injection point for
            tests; when omitted one is built from ``api_key``.

    Returns:
        A dlt source for ``cognee.remember(...)`` with the default
        ``write_disposition="replace"``. Every run is a full snapshot, so
        deleted messages are forgotten automatically.
    """
    region = (region or os.environ.get("CUSTOMERIO_REGION") or "us").lower()
    if region not in _BASE_URLS:
        raise ValueError(f'Customer.io region must be "us" or "eu", got {region!r}.')
    if _NEVER_INGESTED.intersection(message_types):
        raise ValueError("Webhook messages can't be ingested: their bodies can carry secrets.")
    if not include_campaigns and not include_newsletters:
        raise ValueError("Nothing to sync: include_campaigns and include_newsletters are both off.")
    if session is None:
        api_key = api_key or os.environ.get("CUSTOMERIO_APP_API_KEY")
        if not api_key:
            raise ValueError(
                "Customer.io App API key required: pass api_key= or set CUSTOMERIO_APP_API_KEY."
            )
        session = _make_session(api_key)
    api = _Api(session, _BASE_URLS[region])
    message_types = tuple(message_types)

    @dlt.resource(name=MESSAGES_TABLE_NAME, primary_key="id", write_disposition="replace")
    def customerio_messages():
        # Full snapshot: any listing failure raises before the load, which
        # leaves staging (and cognee's memory) untouched.
        yield from sync_messages(api, message_types, include_campaigns, include_newsletters)

    @dlt.source(name=CUSTOMERIO_SOURCE_NAME)
    def _customerio():
        return customerio_messages

    source = _customerio()
    # Opt into the document ingestion path (row → text document → cognify).
    setattr(source, DOCUMENT_SOURCE_ATTR, CUSTOMERIO_SOURCE_NAME)
    return source
