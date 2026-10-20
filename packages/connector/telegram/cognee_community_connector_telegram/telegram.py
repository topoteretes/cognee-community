"""DLT resource for Telegram chats (incremental merge + hard-delete tombstones).

Pulls group and channel messages through the Telegram Bot API ``getUpdates``
endpoint and yields them as a dlt resource for cognee's ingestion pipeline.

Messages are ingested as *normal documents*: the resource declares
``cognee_document_source = "telegram"``, so ``resolve_dlt_sources`` tags each
row ``system_metadata["source"] = "telegram"`` (not ``"dlt"``).
``is_dlt_sourced`` therefore returns False and each message flows through the
standard cognify entity-extraction pipeline — the right treatment for prose —
instead of the deterministic dlt-row schema-context path.

Sync model (mirrors the Gmail connector):

* **Primary key** — ``"<chat_id>:<message_id>"``. With
  ``write_disposition="merge"`` this gives idempotent upserts: an edited
  message (delivered as ``edited_message``/``edited_channel_post``) rewrites
  the same row instead of duplicating it.
* **Incremental cursor** — the Bot API ``update_id`` offset, kept in dlt
  resource state (``state["last_update_id"]``). The first run backfills from
  offset 0; later runs fetch only updates the bot has not seen yet.
* **Forget-on-delete** — the schema carries an ``_deleted`` hard-delete
  column (same contract as Gmail/Drive), but Telegram's Bot API delivers no
  deletion events, so live sync never emits tombstones today. Edited messages
  do sync; to forget a chat entirely, drop its dataset (see README).

Watch out for (from the issue): bots only see messages sent after they join a
chat, and Bot API privacy mode hides most group messages until it is disabled
for the bot via @BotFather.
"""

import os
from datetime import UTC, datetime

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("telegram_connector")

# dlt resource / staging-table name for chat messages.
TELEGRAM_TABLE_NAME = "telegram_messages"
TELEGRAM_SOURCE_NAME = "telegram"

_API_BASE = "https://api.telegram.org"
_PAGE_LIMIT = 100

# Retry budget for rate-limited / transient Bot API responses.
_MAX_RETRIES = 5

_EXTRA_HINT = (
    "The Telegram connector requires the 'telegram' extra. Install it with:\n"
    '    pip install "cognee-community-connector-telegram"\n'
    "(provides dlt; the Bot API needs no SDK — plain HTTPS via httpx)."
)

# Update payloads that carry a message (regular, edited, channel, edited channel).
_MESSAGE_KEYS = ("message", "edited_message", "channel_post", "edited_channel_post")


class TelegramClient:
    """Minimal Telegram Bot API wrapper (``getUpdates`` only).

    ``http_get`` is an injectable ``(url, params) -> response`` callable whose
    response exposes ``.status_code`` and ``.json()`` — the seam tests use to
    fake the API without network.
    """

    def __init__(self, token, http_get=None):
        self._token = token
        self._http_get = http_get or self._default_http_get

    def get_updates(self, offset=None, limit=_PAGE_LIMIT, timeout=30):
        """Return raw update dicts; ``offset`` is an update_id floor (or None)."""
        params = {"limit": limit, "timeout": timeout}
        if offset:
            params["offset"] = offset
        payload = _request(self._http_get, f"{_API_BASE}/bot{self._token}/getUpdates", params)
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API error: {payload.get('description')}")
        return payload.get("result", [])

    @staticmethod
    def _default_http_get(url, params):
        import httpx

        response = httpx.get(url, params=params, timeout=60.0)
        return response


def build_telegram_client(token):
    """Build a live Bot API client from a bot token."""
    return TelegramClient(token)


def telegram_source(*, bot_token=None, chat_ids=None, limit=None, client=None):
    """Return a ``dlt`` resource that yields Telegram messages for ``remember``.

    Args:
        bot_token: Bot token from @BotFather. Falls back to
            ``TELEGRAM_BOT_TOKEN``.
        chat_ids: Restrict ingestion to these chat ids (ints). When omitted,
            every chat the bot can see is ingested.
        limit: Cap the number of updates pulled per run (handy for
            demos/tests). ``None`` = no cap.
        client: Pre-built :class:`TelegramClient`. Mainly an injection point
            for tests; when omitted one is built from the token above.

    Returns:
        A ``dlt`` resource (``telegram_messages``) configured with
        ``primary_key="id"``, ``write_disposition="merge"`` and an ``_deleted``
        hard-delete column. Hand it to ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    resolved_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN")
    if client is None and not resolved_token:
        raise ValueError("Telegram bot token required: pass bot_token= or set TELEGRAM_BOT_TOKEN.")

    scope = set(chat_ids) if chat_ids is not None else None

    @dlt.resource(
        name=TELEGRAM_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        # _deleted is a boolean hard-delete marker: rows where it is True are
        # removed from the dlt destination on merge, which propagates the
        # deletion through cognee's orphan_cleanup. (The Bot API delivers no
        # deletion events, so live sync never emits tombstones today — see the
        # module docstring. The column keeps the family contract for when a
        # delete feed exists.)
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def telegram_messages():
        api = client or build_telegram_client(resolved_token)
        resource_state = dlt.current.resource_state()

        offset = resource_state.get("last_update_id", 0)
        seen = 0
        pulled = 0
        while True:
            page_limit = _PAGE_LIMIT if limit is None else min(_PAGE_LIMIT, limit - pulled)
            if page_limit <= 0:
                break
            updates = api.get_updates(offset=offset or None, limit=page_limit)
            if not updates:
                break
            for update in updates:
                offset = update.get("update_id", 0) + 1
                row = _update_to_row(update, scope)
                if row is None:
                    continue
                seen += 1
                yield row
            pulled += len(updates)
            if len(updates) < page_limit:
                break

        resource_state["last_update_id"] = offset
        logger.info("Telegram: synced %d message(s) (next offset %d).", seen, offset)

    # Opt into the document ingestion path (message → text document → cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(telegram_messages, DOCUMENT_SOURCE_ATTR, TELEGRAM_SOURCE_NAME)
    return telegram_messages


# ---------------------------------------------------------------------------
# Bot API helpers (module-private)
# ---------------------------------------------------------------------------


def _request(http_get, url, params):
    """GET a Bot API method, retrying rate-limit / transient errors.

    Telegram signals rate limits with HTTP 429 (often with a ``retry_after``
    body field); 5xx/timeouts/network errors are transient too. Permanent
    errors (bad token → 401, bad method → 404) propagate so the caller sees a
    misconfiguration instead of an empty sync.
    """
    import time

    import httpx

    last_error: Exception | None = None
    retry_after: float | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            response = http_get(url, params)
        except httpx.TransportError as exc:
            last_error = exc
            retry_after = None
        else:
            if response.status_code == 200:
                return response.json()
            if response.status_code == 429 or 500 <= response.status_code <= 599:
                last_error = RuntimeError(f"Telegram HTTP {response.status_code}")
                retry_after = _response_retry_after(response)
            else:
                raise RuntimeError(
                    f"Telegram request failed (HTTP {response.status_code}): "
                    f"{url.split('/bot')[0]}/bot<redacted>/{url.rsplit('/', 1)[-1]}"
                )
        delay = _retry_delay(retry_after, attempt)
        logger.warning(
            "Telegram: %s — retrying in %.1fs (%d/%d).",
            last_error,
            delay,
            attempt + 1,
            _MAX_RETRIES,
        )
        time.sleep(delay)
    raise RuntimeError(f"Telegram request failed after {_MAX_RETRIES} attempts: {last_error}")


def _response_retry_after(response) -> float | None:
    """The API's ``parameters.retry_after`` hint (seconds), if present."""
    try:
        hint = (response.json().get("parameters") or {}).get("retry_after")
        return float(hint)
    except (TypeError, ValueError, AttributeError):
        return None


def _retry_delay(retry_after: float | None, attempt: int) -> float:
    """Seconds to wait: the API's ``retry_after`` hint, else backoff."""
    if retry_after is not None:
        return retry_after
    return float(2**attempt)


def _update_to_row(update, scope):
    """Flatten one Bot API update into a document row (or None to skip).

    Only ``id``/provenance + ``text`` are kept, so a metadata-only change does
    not churn the content-hash data_id. ``_deleted`` is always False — the Bot
    API delivers no deletion events (see module docstring).
    """
    message = next((update.get(key) for key in _MESSAGE_KEYS if update.get(key)), None)
    if not isinstance(message, dict):
        return None
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if scope is not None and chat_id not in scope:
        return None
    text = message.get("text") or message.get("caption") or ""
    if not text.strip():
        return None  # service/attachment-only updates carry no ingestible text
    sender = message.get("from") or {}
    sender_name = (
        sender.get("username")
        or " ".join(p for p in (sender.get("first_name"), sender.get("last_name")) if p)
        or chat.get("title")
        or "unknown"
    )
    try:
        sent_at = datetime.fromtimestamp(int(message.get("date", 0)), tz=UTC).isoformat()
    except (TypeError, ValueError):
        sent_at = ""
    return {
        "id": f"{chat_id}:{message.get('message_id')}",
        "update_id": update.get("update_id"),
        "chat_id": chat_id,
        "chat_title": chat.get("title") or chat.get("username") or "private",
        "sender": sender_name,
        "date": sent_at,
        # Named "content" (not "text"): the document ingestion path builds the
        # text document from the row's title/content columns.
        "content": text,
        "_deleted": False,
    }
