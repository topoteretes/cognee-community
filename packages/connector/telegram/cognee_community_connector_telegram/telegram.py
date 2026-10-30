"""Telegram connector for cognee: a ``dlt`` source that turns group and channel
messages seen by a bot into memory.

Hand the source to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_telegram import telegram_source

    await cognee.remember(
        telegram_source(token="123456:ABC...", chats=[-1001234567890, "@my_channel"]),
        dataset_name="telegram",
        primary_key="id",
        write_disposition="merge",   # REQUIRED, see below
        max_rows_per_table=0,        # compare forget-on-delete against every row
    )

Design
------
* **Auth**: a bot token from @BotFather, passed as ``token=``. It is checked once
  with ``getMe`` so a wrong token fails immediately. The token is only ever sent
  to ``api.telegram.org`` and never logged, and errors never carry the request URL
  (the token is part of it).
* **One page per run**: asking ``getUpdates`` for the next page confirms the
  previous one on Telegram's side, and confirmed updates are gone for good. So a
  run makes a single ``getUpdates`` call (up to 100 updates), and those updates are
  only confirmed by the *next* run, whose offset comes from the state dlt saved
  after this run's load succeeded. A failed load, or a host revoking access
  mid-run, therefore never loses an update: the next run gets the same page again.
  A busy bot catches up over a few runs.
* **Incremental cursor**: the Bot API ``update_id``, kept in dlt resource state.
  After a week without updates Telegram restarts ``update_id`` at a random value,
  possibly below the saved cursor, so after six quiet days the connector asks
  without an offset (which confirms nothing) instead of risking dropping them.
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
* **Several bots**: two bots in one chat see the same ``chat_id:message_id`` keys,
  so each bot writes its own table and sync state, named after ``resource_name``
  (``telegram_<bot id>`` by default). Otherwise removing one bot from a chat would
  forget the messages the other bot still sees. A sync state written by another
  bot is refused. A state left by the first version of this connector (one
  ``telegram_messages`` table per bot) is adopted, and its table kept.

.. important::
   ``write_disposition="merge"`` is mandatory. The add pipeline defaults to
   ``"replace"``, which would wipe every message from earlier syncs on the second
   run, because ``getUpdates`` only returns *new* updates.

Bot API limits
--------------
* A bot only sees messages sent after it joined (no history backfill), and
  in groups only what privacy mode allows (disable it in @BotFather, or make the
  bot an admin, to see all messages). The connector warns when privacy mode is on.
* Telegram keeps unconfirmed updates for about 24 hours, so sync at least daily.
* ``getUpdates`` fails with 409 while the bot has a webhook, or while another
  process polls the same bot. The connector raises a clear error saying which,
  instead of deleting someone else's webhook; use a dedicated bot.
* Flood waits longer than a minute are not slept through: the run stops without
  rows or a state change, reports ``failed_rate_limit``, and the next sync resumes.
"""

from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timezone
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import (
    DOCUMENT_SOURCE_ATTR,
    PIPELINE_SCOPE_ATTR,
    guarded_rows,
)

logger = get_logger("telegram_connector")

TELEGRAM_SOURCE_NAME = "telegram"
# The table (and state) name of the first version of this connector.
LEGACY_RESOURCE_NAME = "telegram_messages"

_API_BASE = "https://api.telegram.org"
_MAX_RETRIES = 5
_PAGE_SIZE = 100  # Bot API maximum for getUpdates
_MAX_RETRY_AFTER = 60.0  # longer flood waits stop the run instead of sleeping
# dlt snake-cases table names, so "Team-Bot" and "team_bot" would share a table while
# keeping separate sync states. Only names dlt leaves unchanged are accepted.
_RESOURCE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_QUIET_RESET_SECONDS = 6 * 24 * 3600  # update_id may restart after a week without updates
_TITLE_LENGTH = 80
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


class TelegramAuthError(TelegramAPIError):
    """Telegram rejected the bot token."""


class TelegramRateLimitError(TelegramAPIError):
    """Telegram asked to wait longer than the connector sleeps (or kept answering 429)."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class TelegramConflictError(TelegramAPIError):
    """``getUpdates`` is unavailable (409): a webhook is set, or another process polls."""


class TelegramWebhookConflictError(TelegramConflictError):
    """``getUpdates`` is unavailable because a webhook is set on the bot."""


# ---------------------------------------------------------------------------
# Minimal Bot API client (stdlib only)
# ---------------------------------------------------------------------------
class TelegramBotClient:
    """Small Bot API client: ``call(method, **params) -> result``.

    Retries 429 (honouring ``retry_after`` up to a minute) and 5xx/network errors
    with backoff; raises :class:`TelegramAPIError` for everything else. The token
    is part of the URL path, so URLs are never logged or put in an error.
    """

    def __init__(
        self, token: str, timeout: float = 30.0, sleep: Callable[[float], None] = time.sleep
    ):
        self._token = token
        self._timeout = timeout
        self._sleep = sleep

    def __repr__(self) -> str:
        # The token is a password: keep it out of any repr, log or traceback.
        return "TelegramBotClient(token=<redacted>)"

    def _open(self, request):
        return urllib.request.urlopen(request, timeout=self._timeout)

    def call(self, method: str, **params: Any) -> Any:
        url = f"{_API_BASE}/bot{self._token}/{method}"
        body = json.dumps({k: v for k, v in params.items() if v is not None}).encode()
        for attempt in range(_MAX_RETRIES):
            request = urllib.request.Request(
                url, data=body, headers={"Content-Type": "application/json"}
            )
            try:
                with self._open(request) as response:
                    payload = json.load(response)
            except urllib.error.HTTPError as exc:
                # Every raise below uses ``from None``: the HTTPError carries the
                # request URL, and the URL carries the bot token.
                payload = _read_error_payload(exc)
                status = exc.code
                description = str(payload.get("description") or "")[:200]
                if status == 409:
                    if "webhook" in description.lower():
                        raise TelegramWebhookConflictError(
                            f"Telegram returned 409 Conflict ({description}): this bot has a "
                            "webhook set, so getUpdates is unavailable. Use a dedicated bot for "
                            "cognee, or remove the webhook yourself (deleteWebhook) if it is "
                            "not in use."
                        ) from None
                    raise TelegramConflictError(
                        f"Telegram returned 409 Conflict ({description}): another process is "
                        "polling this bot with getUpdates. Stop it, or use a dedicated bot "
                        "for cognee."
                    ) from None
                if status in (401, 404):
                    raise TelegramAuthError(
                        "Telegram rejected the bot token. Create one with @BotFather and pass "
                        "it as token=."
                    ) from None
                if status == 429:
                    retry_after = _retry_after(payload)
                    if retry_after is not None and retry_after > _MAX_RETRY_AFTER:
                        raise TelegramRateLimitError(
                            f"Telegram {method}: flood wait of {retry_after:.0f}s.", retry_after
                        ) from None
                    if attempt == _MAX_RETRIES - 1:
                        raise TelegramRateLimitError(
                            f"Telegram {method}: still rate limited after {_MAX_RETRIES} tries.",
                            retry_after,
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
                    self._sleep(delay)
                    continue
                raise TelegramAPIError(
                    f"Telegram {method} failed: HTTP {status} {description}"
                ) from None
            except ValueError:
                # A 200 whose body is not JSON (a proxy's error page, a cut-off reply).
                # ``from None``: the decode error keeps the whole body (``.doc``).
                raise TelegramAPIError(
                    f"Telegram {method} returned a body that is not JSON."
                ) from None
            except (OSError, http.client.HTTPException) as exc:
                # URLError, timeouts, ConnectionResetError, RemoteDisconnected and
                # IncompleteRead: a dropped connection is retried like a 5xx.
                if attempt < _MAX_RETRIES - 1:
                    delay = float(2**attempt)
                    logger.warning("Telegram %s: network error, retrying in %.1fs.", method, delay)
                    self._sleep(delay)
                    continue
                # ``from None``: the error's text can include the URL, and so the token.
                reason = getattr(exc, "reason", exc)
                raise TelegramAPIError(
                    f"Telegram {method} failed: {type(reason).__name__}"
                ) from None

            if not payload.get("ok"):
                raise TelegramAPIError(
                    f"Telegram {method} failed: {payload.get('description', 'unknown error')}"
                )
            return payload.get("result")
        raise TelegramAPIError(f"Telegram {method} failed after {_MAX_RETRIES} attempts.")


class _CheckedClient:
    """Runs the host's ``check_active`` before every Bot API call."""

    def __init__(self, client, check_active: Callable[[], None]):
        self._client = client
        self._check_active = check_active

    def __repr__(self) -> str:
        return f"_CheckedClient({self._client!r})"

    def call(self, method: str, **params: Any) -> Any:
        self._check_active()
        return self._client.call(method, **params)


def _read_error_payload(exc: urllib.error.HTTPError) -> dict:
    try:
        return json.loads(exc.read().decode() or "{}")
    except (ValueError, OSError):
        return {}


def _retry_after(payload: dict) -> float | None:
    value = (payload.get("parameters") or {}).get("retry_after")
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def _retry_delay(payload: dict, attempt: int) -> float:
    retry_after = _retry_after(payload)
    return retry_after if retry_after is not None else float(2**attempt)


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


def _log_label(chat: dict) -> str:
    """How a chat is named in logs: a private chat's name is a person's name."""
    return "private chat" if chat.get("type") == "private" else _chat_label(chat)


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
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


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
        "title": _title(text, chat_name, message_id),
        "content": content,
        "url": _message_url(chat, message_id),
        "_deleted": False,
    }


def _title(text: str, chat_name: str, message_id) -> str:
    """The message's first line (shortened), which says more than a message number."""
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if not first:
        return f"{chat_name}: message {message_id}"
    return first if len(first) <= _TITLE_LENGTH else first[: _TITLE_LENGTH - 1].rstrip() + "…"


def deleted_row(key: str) -> dict[str, Any]:
    """Hard-delete marker for a stored message."""
    return {"id": key, "_deleted": True}


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


def _selected(chat: dict, ids, usernames, migrated_from: dict[str, str]) -> bool:
    if ids is None:
        return True
    chat_id = chat.get("id")
    if chat_id in ids:
        return True
    if (chat.get("username") or "").lower() in usernames:
        return True
    # A selected group that was upgraded keeps syncing under its new id.
    old = migrated_from.get(str(chat_id))
    return old is not None and int(old) in ids


def fetch_page(client, state: dict, limit: int = _PAGE_SIZE, now: float | None = None) -> list:
    """The one ``getUpdates`` call of a run.

    The offset confirms everything before it, which is safe here: those updates
    were loaded by an earlier run whose state dlt saved. After six days without
    updates the offset is left out, because Telegram may restart ``update_id``
    below the saved cursor after a quiet week, and an offset above the new ids
    would confirm (drop) them. Without an offset nothing is confirmed.
    """
    now = time.time() if now is None else now
    last = state.get("last_update_id")
    seen_at = state.get("last_update_seen_at")
    quiet = seen_at is not None and now - seen_at > _QUIET_RESET_SECONDS
    offset = last + 1 if last is not None and not quiet else None
    if quiet:
        logger.info("Telegram: no updates for over six days; reading without an offset.")
    return (
        client.call(
            "getUpdates",
            offset=offset,
            limit=max(1, min(limit, _PAGE_SIZE)),
            timeout=0,
            allowed_updates=_ALLOWED_UPDATES,
        )
        or []
    )


def sync_updates(
    client,
    state: dict,
    chats: Iterable[int | str] | None = None,
    max_updates: int = _PAGE_SIZE,
    stats: dict | None = None,
    now: float | None = None,
) -> Iterator[dict[str, Any]]:
    """Fetch one page of updates and turn it into rows (``fetch_page`` + ``process_updates``)."""
    now = time.time() if now is None else now
    updates = fetch_page(client, state, max_updates, now)
    yield from process_updates(updates, state, chats, max_updates, stats, now)


def process_updates(
    updates: list,
    state: dict,
    chats: Iterable[int | str] | None = None,
    max_updates: int = _PAGE_SIZE,
    stats: dict | None = None,
    now: float | None = None,
) -> Iterator[dict[str, Any]]:
    """Turn one page of updates into message rows and hard-delete rows.

    ``state`` (dlt resource state) holds ``last_update_id``, the stored message
    keys per chat (``known``) and group-upgrade links (``migrated_from``). The
    run works on copies and writes the state only after its last row, and dlt
    only persists it after the load succeeds, so a failed run is retried from
    the same cursor.
    """
    if stats is None:
        stats = {}
    now = time.time() if now is None else now
    ids, usernames = _normalize_selection(chats)
    known: dict[str, list[int]] = {k: list(v) for k, v in state.get("known", {}).items()}
    migrated_from: dict[str, str] = dict(state.get("migrated_from", {}))
    known_sets: dict[str, set[int]] = {}  # fast membership checks for this run
    rows: dict[str, dict] = {}
    seen_chats: dict[int, str] = {}  # logged at the end, to help pick chat ids
    last_update_id = updates[-1]["update_id"] if updates else state.get("last_update_id")

    for update in updates:
        member = update.get("my_chat_member")
        if member:
            chat = member.get("chat") or {}
            status = (member.get("new_chat_member") or {}).get("status")
            if status in _REMOVED_STATUSES and _selected(chat, ids, usernames, migrated_from):
                for chat_id in _chat_and_aliases(chat.get("id"), migrated_from):
                    known_sets.pop(str(chat_id), None)
                    for message_id in known.pop(str(chat_id), []):
                        key = row_key(chat_id, message_id)
                        rows[key] = deleted_row(key)
                logger.info(
                    "Telegram: bot removed from %s; forgetting its messages.", _log_label(chat)
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
            seen_chats[chat["id"]] = _log_label(chat)
        if not _selected(chat, ids, usernames, migrated_from):
            stats["skipped"] = stats.get("skipped", 0) + 1
            continue
        row = message_to_row(message)
        if row is None:
            stats["skipped"] = stats.get("skipped", 0) + 1
            continue
        rows[row["id"]] = row  # a later edit in the same batch wins
        chat_key = str(chat.get("id"))
        message_id = message.get("message_id")
        seen = known_sets.setdefault(chat_key, set(known.get(chat_key, [])))
        if message_id not in seen:
            seen.add(message_id)
            known.setdefault(chat_key, []).append(message_id)

    if seen_chats:
        logger.info(
            "Telegram: chats seen in this run: %s.",
            ", ".join(f"{label} ({chat_id})" for chat_id, label in seen_chats.items()),
        )
    live = sum(1 for r in rows.values() if not r["_deleted"])
    stats["messages"] = stats.get("messages", 0) + live
    stats["deletions"] = stats.get("deletions", 0) + len(rows) - live
    logger.info("Telegram: %d message row(s), %d deletion(s).", live, len(rows) - live)
    if len(updates) >= max(1, min(max_updates, _PAGE_SIZE)):
        # Telegram drops updates it has not delivered after about 24 hours, so a
        # backlog that keeps up with the runs is the difference between late and lost.
        logger.warning(
            "Telegram: a full page of %d updates was read and more are waiting. The next "
            "run continues; sync more often if this repeats, since Telegram drops "
            "undelivered updates after about 24 hours.",
            len(updates),
        )
    yield from rows.values()

    # Only reached once every row is out: a run stopped mid-way saves nothing.
    if last_update_id is not None:
        state["last_update_id"] = last_update_id
    if updates:
        state["last_update_seen_at"] = now
    state["known"] = known
    state["migrated_from"] = migrated_from


def _chat_and_aliases(chat_id, migrated_from: dict[str, str]) -> list[int]:
    """The chat id plus every older id it was upgraded from (cycles stop the walk)."""
    out: list[int] = []
    seen: set[str] = set()
    current = None if chat_id is None else str(chat_id)
    while current is not None and current not in seen:
        seen.add(current)
        out.append(int(current))
        current = migrated_from.get(current)
    return out


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def telegram_source(
    token: str | None = None,
    chats: Iterable[int | str] | None = None,
    max_updates_per_run: int = _PAGE_SIZE,
    resource_name: str | None = None,
    check_active: Callable[[], None] | None = None,
    client: Any = None,
    service: Any = None,
):
    """Create a dlt source that yields Telegram messages as documents.

    Args:
        token: Bot token from @BotFather.
        chats: Chats to ingest, by numeric id (e.g. ``-1001234567890``) or
            ``@username``. ``None`` ingests every chat the bot is in, private
            messages to the bot included. Updates from other chats are still
            consumed (use a dedicated bot), and dropping a chat from ``chats``
            later leaves its messages in memory.
        max_updates_per_run: Updates read per run, 1 to 100 (one ``getUpdates``
            page, see the module notes). A busy bot catches up over several runs.
        resource_name: Table name and part of the sync state key. Defaults to
            ``telegram_<bot id>`` (the part of the token before ``:``), so two
            bots in one dataset never share a table. Keep it fixed across runs.
        check_active: Optional host authorization checkpoint, called before every
            Bot API call and around every row (``dlt_utils.guarded_rows``);
            raising from it stops the sync.
        client: Object with ``call(method, **params)``; a test injection point.
            Built from the token when omitted. ``service`` is an alias.

    Returns:
        A dlt source with one ``<resource_name>`` resource
        (``primary_key="id"``, ``write_disposition="merge"``, ``_deleted`` hard
        delete). Pass ``write_disposition="merge"`` to ``cognee.remember`` too.
        ``source.cognee_sync_stats`` holds counts only: ``messages``,
        ``deletions``, ``skipped``, ``failed``, ``failed_auth`` and
        ``failed_rate_limit``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Telegram connector requires dlt: pip install "dlt[sqlalchemy]".'
        ) from exc

    if client is not None and service is not None and client is not service:
        raise ValueError("Pass client= or service=, not both.")
    client = client if client is not None else service
    if not isinstance(max_updates_per_run, int) or not 1 <= max_updates_per_run <= _PAGE_SIZE:
        raise ValueError("max_updates_per_run must be between 1 and 100 (one getUpdates page).")
    chats = list(chats) if chats is not None else None
    token = (token or "").strip() or None
    if resource_name and not _RESOURCE_NAME_RE.match(resource_name):
        raise ValueError(
            "resource_name must be lowercase letters, digits and underscores, starting with a "
            "letter, so dlt uses it unchanged as the table name."
        )
    if client is None:
        if not token:
            raise ValueError("Telegram bot token required: pass token= (from @BotFather).")
        client = TelegramBotClient(token)
    # The part of a bot token before ":" is the bot's public numeric id.
    bot_id = token.split(":", 1)[0] if token else "default"
    default_name = f"telegram_{bot_id}"
    resource_name = resource_name or default_name
    if check_active is not None:
        client = _CheckedClient(client, check_active)
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns={
            "_deleted": {"data_type": "bool", "hard_delete": True},
            # Typed up front so a column that is empty in a run (``url`` for
            # private chats, the text columns in a deletion-only run) still exists.
            "title": {"data_type": "text", "nullable": True},
            "content": {"data_type": "text", "nullable": True},
            "url": {"data_type": "text", "nullable": True},
        },
    )
    def telegram_messages():
        yield from guarded_rows(_messages(dlt.current.resource_state()), check_active)

    def _messages(state):
        stats.clear()
        stats.update(messages=0, deletions=0, skipped=0, failed=0)
        table = _adopt_legacy_state(state, resource_name)
        try:
            me = client.call("getMe") or {}  # fail fast on a bad token
            _check_owner(state, me)
            if me.get("can_read_all_group_messages") is False:
                logger.warning(
                    "Telegram: privacy mode is on, so in groups this bot only sees commands "
                    "and replies to it. Disable it in @BotFather (/setprivacy) or make the bot "
                    "an admin to sync every message."
                )
            now = time.time()
            updates = fetch_page(client, state, max_updates_per_run, now)
        except TelegramAuthError:
            stats.update(failed=1, failed_auth=1)
            raise
        except TelegramRateLimitError as exc:
            # Stop cleanly: nothing was confirmed and no state moves, so the next
            # sync reads the same page. Raising would only fail the host's run.
            stats.update(failed=1, failed_rate_limit=1)
            logger.warning(
                "Telegram: rate limited%s; stopping this run, the next sync resumes.",
                f" for {exc.retry_after:.0f}s" if exc.retry_after else "",
            )
            return
        # The state is written after the last row, so a run stopped between rows
        # (by check_active, say) saves nothing and the next run reads this page again.
        for row in process_updates(updates, state, chats, max_updates_per_run, stats, now):
            yield dlt.mark.with_table_name(row, table) if table else row

    def _adopt_legacy_state(state: dict, name: str) -> str | None:
        """Keep a sync from the first version (``telegram_messages``) going.

        That version wrote one ``telegram_messages`` table in a pipeline scoped to
        the bot, the same scope this default uses. Its state is adopted once, and
        its rows keep going to that table, so edits and removals still reach them.
        """
        if state.get("table"):
            return state["table"]
        if name != default_name or state.get("last_update_id") is not None:
            return None
        resources = dlt.current.source_state().get("resources", {})
        legacy = resources.get(LEGACY_RESOURCE_NAME)
        if not legacy or legacy.get("last_update_id") is None:
            return None
        state.update(
            last_update_id=legacy["last_update_id"],
            known={k: list(v) for k, v in legacy.get("known", {}).items()},
            migrated_from=dict(legacy.get("migrated_from", {})),
            table=LEGACY_RESOURCE_NAME,
        )
        logger.info("Telegram: continuing the sync state of %s.", LEGACY_RESOURCE_NAME)
        return LEGACY_RESOURCE_NAME

    def _check_owner(state: dict, me: dict) -> None:
        owner, current = state.get("bot_id"), str(me.get("id") or bot_id)
        if owner is not None and owner != current:
            raise ValueError(
                f"This sync state ('{resource_name}') belongs to another bot. Give each bot "
                "its own resource_name."
            )
        state["bot_id"] = current

    @dlt.source(name=TELEGRAM_SOURCE_NAME)
    def _telegram():
        return telegram_messages

    source = _telegram()
    # Route rows through the document path (text -> cognify), like Notion.
    setattr(source, DOCUMENT_SOURCE_ATTR, TELEGRAM_SOURCE_NAME)
    # Keep the update cursor per bot by default (the same scope as the first
    # version), and per name when one is chosen, so two bots given the same
    # name share one state and the second is refused instead of mixing tables.
    scope = f"telegram:{bot_id}" if resource_name == default_name else f"telegram:{resource_name}"
    setattr(source, PIPELINE_SCOPE_ATTR, scope)
    # Host-readable diagnostics contain counts only, never chat names or text.
    source.cognee_sync_stats = stats
    source.resources[resource_name].cognee_sync_stats = stats
    return source
