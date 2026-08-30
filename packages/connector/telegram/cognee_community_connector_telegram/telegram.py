"""Telegram connector for cognee — a ``dlt`` source over the Bot API update stream.

Sync group and channel messages seen by a Telegram bot into cognee,
incrementally via the Bot API's ``update_id`` offset.  Built on the existing
DLT ingestion subsystem like the sibling Obsidian / Google Drive / Confluence
connectors; the resource produced here is handed directly to
:func:`cognee.remember`::

    import cognee
    from cognee_community_connector_telegram import telegram_source

    await cognee.remember(
        telegram_source(chats=["@my_channel", -1001234567890]),
        dataset_name="telegram",
        primary_key="id",
        write_disposition="merge",   # incremental upsert by chat/message id
        max_rows_per_table=0,        # 0 = no row cap (busy chats exceed the default 50)
    )

Design
------
* **Auth** — a bot token (``TELEGRAM_BOT_TOKEN``), exactly as the issue asks.
  No OAuth dance, no user session; the connector is read-only (``getUpdates``
  is the only method it calls).
* **Primary key** — ``"{chat_id}/{message_id}"``.  Telegram message ids are
  only unique per chat, so the chat id is part of the identity.
* **Ingest** — ``message`` and ``channel_post`` updates whose chat passes the
  optional ``chats`` filter (ids or ``@usernames``).  Text or media caption
  becomes the content; sender, date, chat, reply-to and forward origin become
  record metadata (kept as a ``metadata`` JSON column and folded into the text
  as a ``Message context`` section so it survives entity extraction, turning
  sender→message and reply chains into graph edges).  Service messages and
  media without a caption carry no prose and are skipped.
* **Incremental cursor** — the Bot API's own ``update_id`` offset, kept in
  dlt's per-resource state (``state["offset"]``): each run requests only
  updates newer than the last one committed.  Alongside it, a per-chat map of
  message-id → content fingerprint powers edit dedup and forget-on-removal.
* **Edits** — ``edited_message`` / ``edited_channel_post`` updates re-emit the
  row, which upserts on ``merge``.  Emission is gated on the rendered-row
  content hash, so Telegram's cosmetic edit signals (e.g. a reaction changing
  ``edit_date`` semantics, or an edit that touched only an inline keyboard) do
  not re-cognify an unchanged message.
* **Forget-on-delete** — the Bot API sends *no* update when a single message
  is deleted, so per-message deletions are honestly not detectable on this
  auth model (the README documents this instead of pretending otherwise).
  What *is* reliably signalled is the source going away as a whole: a
  ``my_chat_member`` update saying the bot was removed (``left`` / ``kicked``)
  from a chat.  On that signal every known message of that chat is emitted as
  a ``_deleted`` hard-delete tombstone, dlt drops the rows on ``merge``, and
  cognee's existing ``orphan_cleanup`` purges them from the graph + vector
  stores — removing the upstream source removes its records on the next sync.
* **Watch out (from the issue)** — bots only see messages sent *after* they
  join a chat, and group privacy mode hides most group messages until it is
  disabled via @BotFather.  Both are properties of the Bot API itself; the
  README carries the setup steps.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("telegram_connector")

# dlt resource / staging-table name for Telegram messages.
TELEGRAM_TABLE_NAME = "telegram_messages"
TELEGRAM_SOURCE_NAME = "telegram"

# Update kinds that carry an ingestable message payload.
_MESSAGE_KEYS = ("message", "channel_post", "edited_message", "edited_channel_post")

# my_chat_member statuses that mean the bot lost access to the chat.
_GONE_STATUSES = frozenset({"left", "kicked"})

# getUpdates page size (Bot API maximum).
_BATCH_LIMIT = 100

# Retry budget for rate-limited / transient Bot API responses.
_MAX_RETRIES = 5


@dataclass(frozen=True)
class _TelegramConfig:
    """Normalized connector configuration (see :func:`_normalize_chats`)."""

    chat_ids: frozenset[int] = frozenset()
    chat_usernames: frozenset[str] = frozenset()  # lowercase, no leading @

    @property
    def unfiltered(self) -> bool:
        return not self.chat_ids and not self.chat_usernames


def _normalize_chats(chats: Iterable[int | str] | None) -> _TelegramConfig:
    """Split a mixed chats list into numeric ids and lowercase usernames.

    Accepts ints, numeric strings (``"-1001234"``), and usernames with or
    without the leading ``@``.  An empty/omitted list means "ingest every chat
    the bot can see".
    """
    ids: set[int] = set()
    names: set[str] = set()
    for chat in chats or ():
        if isinstance(chat, int):
            ids.add(chat)
            continue
        text = str(chat).strip()
        if not text:
            continue
        try:
            ids.add(int(text))
        except ValueError:
            names.add(text.lstrip("@").lower())
    return _TelegramConfig(chat_ids=frozenset(ids), chat_usernames=frozenset(names))


def _chat_matches(chat: dict[str, Any], config: _TelegramConfig) -> bool:
    """True when the message's chat passes the configured filter."""
    if config.unfiltered:
        return True
    if chat.get("id") in config.chat_ids:
        return True
    username = str(chat.get("username") or "").lower()
    return bool(username) and username in config.chat_usernames


# ---------------------------------------------------------------------------
# Row rendering
# ---------------------------------------------------------------------------
def _sender_name(message: dict[str, Any]) -> str:
    """Human-readable sender: user name, else channel signature/chat title."""
    user = message.get("from") or {}
    if user:
        name = " ".join(part for part in (user.get("first_name"), user.get("last_name")) if part)
        return name or (f"@{user['username']}" if user.get("username") else str(user.get("id", "")))
    if message.get("author_signature"):
        return str(message["author_signature"])
    chat = message.get("chat") or {}
    return str(chat.get("title") or chat.get("username") or "")


def _media_kind(message: dict[str, Any]) -> str | None:
    """The media type attached to the message, if any."""
    for kind in ("photo", "video", "document", "audio", "voice", "animation", "sticker", "poll"):
        if kind in message:
            return kind
    return None


def _forward_origin(message: dict[str, Any]) -> str | None:
    """Human-readable origin of a forwarded message, or None."""
    origin = message.get("forward_origin")
    if not isinstance(origin, dict):
        return None
    sender = origin.get("sender_user") or {}
    chat = origin.get("chat") or origin.get("sender_chat") or {}
    return (
        " ".join(part for part in (sender.get("first_name"), sender.get("last_name")) if part)
        or origin.get("sender_user_name")
        or chat.get("title")
        or chat.get("username")
        or origin.get("type")
    )


def _message_url(chat: dict[str, Any], message_id: int) -> str:
    """Best provenance URL Telegram offers for this chat type.

    Public chats/channels get a real ``t.me`` link; private supergroups and
    channels get the ``t.me/c/<internal id>`` form (works for members); basic
    private groups have no web link at all, so a deterministic ``telegram://``
    identifier stands in — provenance, not a promise it opens in a browser.
    """
    if chat.get("username"):
        return f"https://t.me/{chat['username']}/{message_id}"
    chat_id = chat.get("id")
    if isinstance(chat_id, int) and str(chat_id).startswith("-100"):
        return f"https://t.me/c/{str(chat_id)[4:]}/{message_id}"
    return f"telegram://chat/{chat_id}/message/{message_id}"


def _iso(timestamp: Any) -> str | None:
    """Render a Bot API unix timestamp as an ISO-8601 UTC string."""
    if not isinstance(timestamp, (int, float)):
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))


def _render_message(message: dict[str, Any]) -> dict[str, Any] | None:
    """Flatten one Bot API message into a document-mode dlt row, or None.

    The row contract is ``{id, title, content, url}`` (what
    ``resolve_dlt_sources`` expects of a document source) plus a structured
    ``metadata`` JSON column.  Sender / chat / reply / forward context is also
    rendered into the content as a ``Message context`` section so entity
    extraction keeps those relationships as graph edges.  Messages with no
    text and no caption (service messages, bare media) carry no prose to
    cognify and are skipped.
    """
    chat = message.get("chat") or {}
    message_id = message.get("message_id")
    if message_id is None or chat.get("id") is None:
        return None

    text = message.get("text") or message.get("caption") or ""
    if not text.strip():
        return None

    sender = _sender_name(message)
    chat_title = str(chat.get("title") or chat.get("username") or chat.get("id"))
    media = _media_kind(message)
    forwarded_from = _forward_origin(message)
    reply_to = (message.get("reply_to_message") or {}).get("message_id")

    context = [f"- chat: {chat_title}"]
    if sender:
        context.append(f"- sender: {sender}")
    if (date := _iso(message.get("date"))) is not None:
        context.append(f"- date: {date}")
    if reply_to is not None:
        context.append(f"- reply to message {reply_to}")
    if forwarded_from:
        context.append(f"- forwarded from: {forwarded_from}")
    if media:
        context.append(f"- attached media: {media} (caption ingested; media file is not)")

    first_line = text.strip().splitlines()[0]
    title = first_line if len(first_line) <= 80 else first_line[:77] + "..."

    metadata = {
        "chat_id": chat.get("id"),
        "chat_title": chat.get("title"),
        "chat_username": chat.get("username"),
        "chat_type": chat.get("type"),
        "sender": sender or None,
        "sender_id": (message.get("from") or {}).get("id"),
        "date": _iso(message.get("date")),
        "edit_date": _iso(message.get("edit_date")),
        "reply_to_message_id": reply_to,
        "forwarded_from": forwarded_from,
        "media": media,
    }

    return {
        "id": f"{chat['id']}/{message_id}",
        "title": title,
        "content": f"{text.strip()}\n\nMessage context:\n" + "\n".join(context),
        "url": _message_url(chat, message_id),
        "metadata": json.dumps(metadata, sort_keys=True, default=str),
        "_deleted": False,
    }


def _row_fingerprint(row: dict[str, Any]) -> str:
    """Content hash of everything the row emits — gates edit re-emission."""
    material = "\x00".join((row["title"], row["content"], row["metadata"], row["url"]))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Sync (pure given config + state + an updates iterable — unit-testable)
# ---------------------------------------------------------------------------
def sync_updates(
    config: _TelegramConfig, state: dict, updates: Iterable[dict[str, Any]]
) -> Iterator[dict[str, Any]]:
    """Yield changed message rows and tombstones for one batch of Bot API updates.

    Pure over ``(config, state, updates)``: no network, no client — the
    caller supplies the raw ``getUpdates`` result items.  ``state`` carries
    ``offset`` (the ``update_id`` cursor: next run asks Telegram only for
    updates past the highest one processed here) and ``chats`` (per-chat
    ``{title, messages: {message_id: fingerprint}}``).  New and edited
    messages re-emit only when their rendered-row fingerprint changed; a
    ``my_chat_member`` removal tombstones every known message of that chat.
    """
    chats: dict[str, dict[str, Any]] = state.setdefault("chats", {})
    offset = int(state.get("offset", 0))
    changed = deleted = 0

    for update in updates:
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            offset = max(offset, update_id + 1)

        member = update.get("my_chat_member")
        if isinstance(member, dict):
            status = ((member.get("new_chat_member") or {}).get("status") or "").lower()
            chat_id = str((member.get("chat") or {}).get("id"))
            known = chats.get(chat_id)
            if status in _GONE_STATUSES and known:
                for message_id in sorted(known.get("messages", {})):
                    deleted += 1
                    yield {"id": f"{chat_id}/{message_id}", "_deleted": True}
                del chats[chat_id]
            continue

        message = next(
            (update[key] for key in _MESSAGE_KEYS if isinstance(update.get(key), dict)), None
        )
        if message is None:
            continue
        chat = message.get("chat") or {}
        if not _chat_matches(chat, config):
            continue

        row = _render_message(message)
        if row is None:
            continue

        chat_state = chats.setdefault(str(chat["id"]), {"title": "", "messages": {}})
        chat_state["title"] = str(chat.get("title") or chat.get("username") or "")
        fingerprint = _row_fingerprint(row)
        message_key = str(message["message_id"])
        if chat_state["messages"].get(message_key) == fingerprint:
            continue  # cosmetic edit signal — content identical, nothing to re-cognify
        chat_state["messages"][message_key] = fingerprint
        changed += 1
        yield row

    state["offset"] = offset
    logger.info("Telegram: %d changed message(s), %d deletion(s).", changed, deleted)


# ---------------------------------------------------------------------------
# Bot API client (getUpdates only — read-only by construction)
# ---------------------------------------------------------------------------
class _BotClient:
    """Minimal ``getUpdates`` client over urllib (no extra dependency)."""

    def __init__(self, token: str, api_root: str = "https://api.telegram.org"):
        self._url = f"{api_root}/bot{token}/getUpdates"

    def get_updates(self, offset: int) -> list[dict[str, Any]]:
        """One ``getUpdates`` page from ``offset``, retrying transient errors.

        Honors the 429 ``retry_after`` parameter; auth errors (401/404 —
        a bad token) raise immediately.
        """
        query = urllib.parse.urlencode(
            {
                "offset": offset,
                "limit": _BATCH_LIMIT,
                "timeout": 0,
                "allowed_updates": json.dumps([*_MESSAGE_KEYS, "my_chat_member"]),
            }
        )
        for attempt in range(_MAX_RETRIES):
            try:
                with urllib.request.urlopen(f"{self._url}?{query}", timeout=60) as response:
                    payload = json.load(response)
            except urllib.error.HTTPError as exc:
                payload = self._error_payload(exc)
                if exc.code in (401, 404):
                    raise ValueError(f"Telegram rejected the bot token: {payload}") from exc
                if attempt == _MAX_RETRIES - 1:
                    raise
                delay = float((payload.get("parameters") or {}).get("retry_after") or 2**attempt)
                logger.warning(
                    "Telegram: HTTP %s — retrying in %.1fs (%d/%d).",
                    exc.code,
                    delay,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(delay)
                continue
            except OSError:
                if attempt == _MAX_RETRIES - 1:
                    raise
                time.sleep(float(2**attempt))
                continue
            if not payload.get("ok"):
                raise ValueError(f"Telegram getUpdates failed: {payload}")
            return payload.get("result") or []
        return []  # pragma: no cover — the loop always returns or raises

    @staticmethod
    def _error_payload(exc) -> dict[str, Any]:
        try:
            return json.loads(exc.read().decode("utf-8", errors="replace"))
        except Exception:
            return {}


def _fetch_all_updates(client: Any, offset: int) -> Iterator[dict[str, Any]]:
    """Drain the bot's pending updates, paging by ``update_id``."""
    while True:
        batch = client.get_updates(offset)
        if not batch:
            return
        yield from batch
        offset = max(
            (u["update_id"] + 1 for u in batch if isinstance(u.get("update_id"), int)),
            default=offset,
        )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def telegram_source(
    token: str | None = None,
    *,
    chats: list[int | str] | None = None,
    client: Any = None,
):
    """Return a ``dlt`` resource yielding one row per Telegram message for ``remember``.

    Args:
        token: Bot token from @BotFather. Falls back to the
            ``TELEGRAM_BOT_TOKEN`` environment variable.
        chats: Chat ids and/or ``@usernames`` to ingest. Omitted or empty
            means every chat the bot receives updates for.
        client: Object with ``get_updates(offset) -> list[update]`` (mainly a
            test-injection point); when omitted a Bot API client is built
            from the token above.

    Returns:
        A ``dlt`` resource (``telegram_messages``) configured with
        ``primary_key="id"``, ``write_disposition="merge"`` and a ``_deleted``
        hard-delete column. Hand it to ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Telegram connector requires the dlt extra: pip install "cognee[dlt]".'
        ) from exc

    if client is None:
        resolved = token or os.getenv("TELEGRAM_BOT_TOKEN")
        if not resolved:
            raise ValueError(
                "Bot token is required (pass token= or set TELEGRAM_BOT_TOKEN). "
                "Get one from @BotFather."
            )
        client = _BotClient(resolved)

    config = _normalize_chats(chats)

    @dlt.resource(
        name=TELEGRAM_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        # `_deleted` is a boolean hard-delete marker (matching the Google
        # Drive / Confluence / Obsidian connectors): rows where it is True are
        # removed from the dlt destination on merge, which propagates the
        # deletion through cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def telegram_messages():
        state = dlt.current.resource_state()
        updates = _fetch_all_updates(client, int(state.get("offset", 0)))
        yield from sync_updates(config, state, updates)

    resource = telegram_messages()
    # Opt into the document ingestion path: each message row (id/title/content/
    # url) becomes a text document that flows through normal cognify (LLM graph
    # extraction). resolve_dlt_sources reads this marker; it never imports this
    # connector. Sync stays incremental — hand this to remember() with
    # write_disposition="merge" (update_id cursor + _deleted hard-delete).
    setattr(resource, DOCUMENT_SOURCE_ATTR, TELEGRAM_SOURCE_NAME)
    return resource
