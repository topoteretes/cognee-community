"""Telegram connector for cognee: a ``dlt`` source that turns group and channel
messages seen by a bot into memory.

Hand the source to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_telegram import telegram_source

    await cognee.remember(
        telegram_source(chats=[-1001234567890, "@my_channel"]),
        dataset_name="telegram",
        primary_key="id",
        write_disposition="merge",   # REQUIRED, see below
        max_rows_per_table=0,        # compare forget-on-delete against every row
    )

Design
------
* **Auth**: a bot token from @BotFather (``TELEGRAM_BOT_TOKEN``). It is checked
  once with ``getMe`` so a wrong token fails immediately. The token is only ever
  sent to ``api.telegram.org`` and is never logged.
* **Incremental cursor**: the Bot API ``update_id``. Each run calls
  ``getUpdates(offset=last_update_id + 1)`` and the new ``last_update_id`` is
  kept in dlt resource state, which dlt only commits after a successful load.
* **Edits**: ``edited_message`` / ``edited_channel_post`` carry the same
  ``message_id``, so they upsert the existing row (``merge`` on ``chat_id:message_id``;
  message ids are only unique inside one chat).
* **Forget-on-delete**: the Bot API sends no event when a message is deleted
  (verified on a live bot: nothing arrives, not even a gap in ``update_id``). It does send
  ``my_chat_member`` with status ``left``/``kicked`` when the bot is removed from a
  chat or the chat is deleted. On that event every message the connector has
  stored for the chat is emitted as a ``_deleted`` hard-delete row, so cognee's
  ``orphan_cleanup`` forgets them. This is chat-level deletion; per-message
  deletion is not observable through a bot token.
* **Group upgrades**: when a group becomes a supergroup Telegram assigns a new
  chat id and restarts message ids at 1 (``migrate_to_chat_id``). Old rows keep
  their old key (re-keying could collide with the new ids); the connector follows
  the new id so a chat selected by id keeps syncing, and removal from the new chat
  forgets the messages stored under both ids.

.. important::
   ``write_disposition="merge"`` is mandatory. The add pipeline defaults to
   ``"replace"``, which would wipe every message from earlier syncs on the second
   run, because ``getUpdates`` only returns *new* updates.

Bot API limits
--------------
* A bot only sees messages sent after it joined (no history backfill), and
  in groups only what privacy mode allows (disable it in @BotFather, or make the
  bot an admin, to see all messages).
* Telegram keeps unconfirmed updates for about 24 hours, so sync at least daily.
* Requesting the next page of updates confirms the previous page on Telegram's
  side. If a run fetches several pages and then fails to load, the updates of the
  earlier pages are not delivered again; pass ``max_updates_per_run=100`` to fetch
  a single page per run if that matters more than throughput.
* ``getUpdates`` fails with 409 while the bot has a webhook. The connector raises
  a clear error instead of deleting someone else's webhook; use a dedicated bot.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

try:  # cognee >= 1.6: per-dataset, per-bot dlt state
    from cognee.tasks.ingestion.dlt_utils import PIPELINE_SCOPE_ATTR
except ImportError:  # cognee 1.4: one shared pipeline state
    PIPELINE_SCOPE_ATTR = None

logger = get_logger("telegram_connector")

TELEGRAM_TABLE_NAME = "telegram_messages"
TELEGRAM_SOURCE_NAME = "telegram"

_API_BASE = "https://api.telegram.org"
_MAX_RETRIES = 5
_PAGE_SIZE = 100  # Bot API maximum for getUpdates
_ALLOWED_UPDATES = [
    "message",
    "edited_message",
    "channel_post",
    "edited_channel_post",
    "my_chat_member",
]
_MESSAGE_KEYS = ("message", "edited_message", "channel_post", "edited_channel_post")
_REMOVED_STATUSES = {"left", "kicked"}


class TelegramAPIError(RuntimeError):
    """A Bot API call failed permanently."""


class TelegramWebhookConflictError(TelegramAPIError):
    """``getUpdates`` is unavailable because a webhook is set on the bot."""


# ---------------------------------------------------------------------------
# Minimal Bot API client (stdlib only)
# ---------------------------------------------------------------------------
class TelegramBotClient:
    """Small Bot API client: ``call(method, **params) -> result``.

    Retries 429 (honouring ``retry_after``) and 5xx/network errors with backoff;
    raises :class:`TelegramAPIError` for everything else. The token is part of
    the URL path, so URLs are never logged.
    """

    def __init__(self, token: str, timeout: float = 30.0):
        self._token = token
        self._timeout = timeout

    def call(self, method: str, **params: Any) -> Any:
        url = f"{_API_BASE}/bot{self._token}/{method}"
        body = json.dumps({k: v for k, v in params.items() if v is not None}).encode()
        for attempt in range(_MAX_RETRIES):
            request = urllib.request.Request(
                url, data=body, headers={"Content-Type": "application/json"}
            )
            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    payload = json.load(response)
            except urllib.error.HTTPError as exc:
                payload = _read_error_payload(exc)
                status = exc.code
                if status == 409:
                    raise TelegramWebhookConflictError(
                        "Telegram returned 409 Conflict: this bot has a webhook set, so "
                        "getUpdates is unavailable. Use a dedicated bot for cognee, or "
                        "remove the webhook yourself (deleteWebhook) if it is not in use."
                    ) from None
                if status == 401 or status == 404:
                    raise TelegramAPIError(
                        "Telegram rejected the bot token (check TELEGRAM_BOT_TOKEN)."
                    ) from None
                if (status == 429 or status >= 500) and attempt < _MAX_RETRIES - 1:
                    delay = _retry_delay(payload, attempt)
                    logger.warning(
                        "Telegram %s: HTTP %s, retrying in %.1fs (%d/%d).",
                        method,
                        status,
                        delay,
                        attempt + 1,
                        _MAX_RETRIES,
                    )
                    time.sleep(delay)
                    continue
                raise TelegramAPIError(
                    f"Telegram {method} failed: HTTP {status} {payload.get('description', '')}"
                ) from None
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < _MAX_RETRIES - 1:
                    delay = float(2**attempt)
                    logger.warning("Telegram %s: network error, retrying in %.1fs.", method, delay)
                    time.sleep(delay)
                    continue
                raise TelegramAPIError(f"Telegram {method} failed: {exc.reason!s}") from None

            if not payload.get("ok"):
                raise TelegramAPIError(
                    f"Telegram {method} failed: {payload.get('description', 'unknown error')}"
                )
            return payload.get("result")
        raise TelegramAPIError(f"Telegram {method} failed after {_MAX_RETRIES} attempts.")


def _read_error_payload(exc: urllib.error.HTTPError) -> dict:
    try:
        return json.loads(exc.read().decode() or "{}")
    except (ValueError, OSError):
        return {}


def _retry_delay(payload: dict, attempt: int) -> float:
    retry_after = (payload.get("parameters") or {}).get("retry_after")
    try:
        return float(retry_after)
    except (TypeError, ValueError):
        return float(2**attempt)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def row_key(chat_id: int, message_id: int) -> str:
    """Primary key of a message row: message ids are only unique inside a chat."""
    return f"{chat_id}:{message_id}"


def _chat_label(chat: dict) -> str:
    return (
        chat.get("title")
        or (f"@{chat['username']}" if chat.get("username") else "")
        or (" ".join(p for p in (chat.get("first_name"), chat.get("last_name")) if p))
        or str(chat.get("id"))
    )


def _user_label(user: dict) -> str:
    name = " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p)
    if user.get("username"):
        name = f"{name} (@{user['username']})" if name else f"@{user['username']}"
    return name or str(user.get("id"))


def _author(message: dict) -> str:
    """Who wrote the message. Channel posts have no ``from``: the channel is the author."""
    sender = message.get("from")
    if sender:
        return _user_label(sender)
    if message.get("sender_chat"):
        return _chat_label(message["sender_chat"])
    return _chat_label(message.get("chat") or {})


def _forwarded_from(message: dict) -> str | None:
    """Original author of a forwarded message, so it is not credited to the forwarder."""
    origin = message.get("forward_origin")
    if not origin:
        return None
    kind = origin.get("type")
    if kind == "user" and origin.get("sender_user"):
        return _user_label(origin["sender_user"])
    if kind == "hidden_user":
        return origin.get("sender_user_name") or "a hidden user"
    if kind == "chat" and origin.get("sender_chat"):
        return _chat_label(origin["sender_chat"])
    if kind == "channel" and origin.get("chat"):
        return _chat_label(origin["chat"])
    return "unknown"


def _message_url(chat: dict, message_id) -> str | None:
    """t.me link: public for chats with a username, members-only for private supergroups
    and channels (``t.me/c/<id>``). Basic groups have no message links."""
    if chat.get("username"):
        return f"https://t.me/{chat['username']}/{message_id}"
    chat_id = str(chat.get("id", ""))
    if chat.get("type") in ("supergroup", "channel") and chat_id.startswith("-100"):
        return f"https://t.me/c/{chat_id[4:]}/{message_id}"
    return None


def _message_text(message: dict) -> str:
    """Text of a message, or the caption of a media message (with its media type)."""
    if message.get("text"):
        return message["text"]
    caption = message.get("caption")
    if not caption:
        return ""
    for media in ("photo", "video", "document", "audio", "voice", "animation"):
        if message.get(media):
            return f"[{media}] {caption}"
    return caption


def _iso(ts: int | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")


def message_to_row(message: dict) -> dict[str, Any] | None:
    """Render one Bot API message as a document row, or ``None`` if it has no text.

    The row is deterministic (no fetch timestamps) so an unchanged message keeps
    the same content hash and is not re-cognified. ``title``/``content``/``url``/``id``
    are the columns cognee's document path reads.
    """
    text = _message_text(message)
    if not text.strip():
        return None  # service messages (joins, pins, ...) and media without captions

    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    message_id = message.get("message_id")
    chat_name = _chat_label(chat)
    lines = [
        f"Chat: {chat_name}",
        f"From: {_author(message)}",
        f"Date: {_iso(message.get('date'))}",
    ]
    forwarded_from = _forwarded_from(message)
    if forwarded_from:
        lines.append(f"Forwarded from: {forwarded_from}")
    if message.get("edit_date"):
        lines.append(f"Edited: {_iso(message['edit_date'])}")
    reply = message.get("reply_to_message")
    if reply:
        quoted = _message_text(reply).strip().replace("\n", " ")
        lines.append(
            f"In reply to {_author(reply)}: {quoted[:200]}"
            if quoted
            else f"In reply to message {reply.get('message_id')}"
        )
    content = "\n".join(lines) + "\n\n" + text

    return {
        "id": row_key(chat_id, message_id),
        "chat_id": chat_id,
        "message_id": message_id,
        "title": f"{chat_name}: message {message_id}",
        "content": content,
        "url": _message_url(chat, message_id),
        "_deleted": False,
    }


def deleted_row(key: str) -> dict[str, Any]:
    """Hard-delete marker for a stored message."""
    chat_id, message_id = key.split(":", 1)
    return {"id": key, "chat_id": int(chat_id), "message_id": int(message_id), "_deleted": True}


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
def _normalize_selection(chats: Iterable[int | str] | None):
    if chats is None:
        return None, None
    ids, usernames = set(), set()
    for chat in chats:
        if isinstance(chat, int) or (isinstance(chat, str) and chat.lstrip("-").isdigit()):
            ids.add(int(chat))
        else:
            usernames.add(str(chat).lstrip("@").lower())
    return ids, usernames


def _selected(chat: dict, ids, usernames, state: dict) -> bool:
    if ids is None:
        return True
    chat_id = chat.get("id")
    if chat_id in ids:
        return True
    if (chat.get("username") or "").lower() in usernames:
        return True
    # A selected group that was upgraded keeps syncing under its new id.
    old = state.get("migrated_from", {}).get(str(chat_id))
    return old is not None and int(old) in ids


def iter_updates(client, state: dict, max_updates: int | None = None) -> Iterator[dict]:
    """Yield updates after ``state['last_update_id']``, page by page."""
    offset = state.get("last_update_id")
    offset = offset + 1 if offset is not None else None
    fetched = 0
    while True:
        limit = _PAGE_SIZE if max_updates is None else min(_PAGE_SIZE, max_updates - fetched)
        if limit <= 0:
            return
        updates = (
            client.call(
                "getUpdates",
                offset=offset,
                limit=limit,
                timeout=0,
                allowed_updates=_ALLOWED_UPDATES,
            )
            or []
        )
        if not updates:
            return
        yield from updates
        fetched += len(updates)
        offset = updates[-1]["update_id"] + 1
        if len(updates) < limit:
            return


def sync_updates(
    client,
    state: dict,
    chats: Iterable[int | str] | None = None,
    max_updates: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Turn new updates into message rows and hard-delete rows.

    ``state`` (dlt resource state) holds ``last_update_id``, the stored message
    keys per chat (``known``) and group-upgrade links (``migrated_from``). It is
    only persisted by dlt after the load succeeds, so a failed run is retried
    from the same cursor.
    """
    ids, usernames = _normalize_selection(chats)
    known: dict[str, list[int]] = state.setdefault("known", {})
    migrated_from: dict[str, str] = state.setdefault("migrated_from", {})
    known_sets: dict[str, set[int]] = {}  # fast membership checks for this run
    rows: dict[str, dict] = {}
    seen_chats: dict[int, str] = {}  # logged at the end, to help pick chat ids
    last_update_id = state.get("last_update_id")

    for update in iter_updates(client, state, max_updates):
        last_update_id = update["update_id"]

        member = update.get("my_chat_member")
        if member:
            chat = member.get("chat") or {}
            status = (member.get("new_chat_member") or {}).get("status")
            if status in _REMOVED_STATUSES and _selected(chat, ids, usernames, state):
                for chat_id in _chat_and_aliases(chat.get("id"), migrated_from):
                    known_sets.pop(str(chat_id), None)
                    for message_id in known.pop(str(chat_id), []):
                        key = row_key(chat_id, message_id)
                        rows[key] = deleted_row(key)
                logger.info(
                    "Telegram: bot removed from %s; forgetting its messages.", _chat_label(chat)
                )
            continue

        message = next((update[k] for k in _MESSAGE_KEYS if update.get(k)), None)
        if message is None:
            continue
        chat = message.get("chat") or {}
        # Telegram announces an upgrade twice: migrate_to_chat_id in the old
        # group and migrate_from_chat_id in the new supergroup. Either one links
        # the ids, so a run that only sees one side still follows the chat.
        if message.get("migrate_to_chat_id"):
            migrated_from[str(message["migrate_to_chat_id"])] = str(chat.get("id"))
        if message.get("migrate_from_chat_id"):
            migrated_from[str(chat.get("id"))] = str(message["migrate_from_chat_id"])
        if chat.get("id") is not None:
            seen_chats[chat["id"]] = _chat_label(chat)
        if not _selected(chat, ids, usernames, state):
            continue
        row = message_to_row(message)
        if row is None:
            continue
        rows[row["id"]] = row  # a later edit in the same batch wins
        chat_key = str(row["chat_id"])
        seen = known_sets.setdefault(chat_key, set(known.get(chat_key, [])))
        if row["message_id"] not in seen:
            seen.add(row["message_id"])
            known.setdefault(chat_key, []).append(row["message_id"])

    if last_update_id is not None:
        state["last_update_id"] = last_update_id
    if seen_chats:
        logger.info(
            "Telegram: chats seen in this run: %s.",
            ", ".join(f"{label} ({chat_id})" for chat_id, label in seen_chats.items()),
        )
    live = sum(1 for r in rows.values() if not r["_deleted"])
    logger.info("Telegram: %d message row(s), %d deletion(s).", live, len(rows) - live)
    yield from rows.values()


def _chat_and_aliases(chat_id, migrated_from: dict[str, str]) -> list[int]:
    """The chat id plus every older id it was upgraded from."""
    out, current = [], chat_id
    while current is not None and current not in out:
        out.append(int(current))
        current = migrated_from.get(str(current))
    return out


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def telegram_source(
    token: str | None = None,
    chats: Iterable[int | str] | None = None,
    max_updates_per_run: int | None = None,
    client: Any = None,
):
    """Create a dlt source that yields Telegram messages as documents.

    Args:
        token: Bot token from @BotFather. Falls back to ``TELEGRAM_BOT_TOKEN``.
        chats: Chats to ingest, by numeric id (e.g. ``-1001234567890``) or
            ``@username``. ``None`` ingests every chat the bot is in. Updates
            from other chats are still consumed (use a dedicated bot).
        max_updates_per_run: Fetch at most this many updates per run
            (``100`` = a single page, see the module notes). ``None`` = all.
        client: Object with ``call(method, **params)``; a test injection point.
            Built from the token when omitted.

    Returns:
        A dlt source with one ``telegram_messages`` resource
        (``primary_key="id"``, ``write_disposition="merge"``, ``_deleted`` hard
        delete). Pass ``write_disposition="merge"`` to ``cognee.remember`` too.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Telegram connector requires dlt: pip install "dlt[sqlalchemy]".'
        ) from exc

    chats = list(chats) if chats is not None else None
    resolved = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    if client is None:
        if not resolved:
            raise ValueError("Telegram bot token required: pass token= or set TELEGRAM_BOT_TOKEN.")
        client = TelegramBotClient(resolved)
    # The part of a bot token before ":" is the bot's public numeric id.
    bot_id = resolved.split(":", 1)[0] if resolved else "default"

    @dlt.resource(
        name=TELEGRAM_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns={
            "_deleted": {"data_type": "bool", "hard_delete": True},
            # Typed up front so a column that is empty in a run (``url`` for
            # private chats, the text columns in a deletion-only run) still exists.
            "title": {"data_type": "text", "nullable": True},
            "content": {"data_type": "text", "nullable": True},
            "url": {"data_type": "text", "nullable": True},
            "chat_id": {"data_type": "bigint"},
            "message_id": {"data_type": "bigint"},
        },
    )
    def telegram_messages():
        me = client.call("getMe")  # fail fast on a bad token
        logger.info("Telegram: syncing as @%s.", (me or {}).get("username", "?"))
        yield from sync_updates(
            client, dlt.current.resource_state(), chats=chats, max_updates=max_updates_per_run
        )

    @dlt.source(name=TELEGRAM_SOURCE_NAME)
    def _telegram():
        return telegram_messages

    source = _telegram()
    # Route rows through the document path (text -> cognify), like Notion.
    setattr(source, DOCUMENT_SOURCE_ATTR, TELEGRAM_SOURCE_NAME)
    # Keep the update cursor per bot (and, in cognee, per dataset), so two bots or
    # datasets never resume from each other's cursor.
    if PIPELINE_SCOPE_ATTR:
        setattr(source, PIPELINE_SCOPE_ATTR, f"telegram:{bot_id}")
    return source
