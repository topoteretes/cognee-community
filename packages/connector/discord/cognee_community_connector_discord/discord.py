"""Discord connector for cognee: a ``dlt`` source that turns a server's conversations into memory.

Sync a Discord server's channel messages, threads and forum posts into cognee,
incrementally and with forget-on-delete. The resource is handed directly to
:func:`cognee.remember`::

    import cognee
    from cognee_community_connector_discord import discord_source

    await cognee.remember(
        discord_source(guild_id="<server id>"),  # DISCORD_BOT_TOKEN from env
        dataset_name="discord",
        primary_key="id",
        write_disposition="merge",  # REQUIRED, the add pipeline defaults to "replace"
        max_rows_per_table=0,
    )

Design
------
* **Auth** is a bot token over the REST API; no Gateway connection is opened.
  Without the Message Content Intent Discord returns empty message content
  instead of an error, so the app flags are checked before anything is read.
* **Rows** are flat ``{id, title, content, url, _deleted}``, one per channel or
  thread per UTC day (``discord:<channel id>:<YYYY-MM-DD>``). A new message only
  re-processes that day's document, and nothing volatile is rendered.
* **Incremental** with the message id (a snowflake, which encodes time) as the
  cursor, paged with ``after=<id>``. Each run re-reads from the start of the UTC
  day of ``min(cursor, now - rescan_days)``, so edits and deletions inside that
  window are caught. Older days are left as they are; ``full_resync`` re-reads
  everything.
* **Forget-on-delete.** A day whose messages were all deleted, a deleted thread
  or forum post, and a deleted or no longer visible channel are emitted as
  hard-delete tombstones. Only definitive answers (unknown channel, missing
  access) delete anything: any other failure raises, and dlt rolls the state of
  a failed run back.

Privacy
-------
This reads every message the bot can see. Nothing is fetched until you construct
a source and call ``remember``. Give the bot a role limited to the channels you
want in memory, and remember that anyone with read access to the target dataset
can read what was ingested.
"""

import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = logging.getLogger(__name__)

API_URL = "https://discord.com/api/v10"
# Discord requires this format and may block requests without it.
USER_AGENT = "DiscordBot (https://github.com/topoteretes/cognee-community, 0.1.0)"
DISCORD_EPOCH_MS = 1420070400000
DEFAULT_SINCE_DAYS = 90
DEFAULT_RESCAN_DAYS = 7
_PAGE_SIZE = 100
_TIMEOUT = 30.0
_RETRIES = 5
_MAX_RETRY_SLEEP = 60.0
_GATEWAY_MESSAGE_CONTENT = 1 << 18
_GATEWAY_MESSAGE_CONTENT_LIMITED = 1 << 19
_TEXT_CHANNEL_TYPES = {0, 5}  # GUILD_TEXT, GUILD_ANNOUNCEMENT
_FORUM_CHANNEL_TYPES = {15, 16}  # GUILD_FORUM, GUILD_MEDIA
_PRIVATE_THREAD = 12
# DEFAULT, REPLY and THREAD_STARTER_MESSAGE; the rest are system messages.
_MESSAGE_TYPES = {0, 19, 21}
_THREAD_STARTER = 21
# Unknown channel, missing access, missing permissions.
_GONE_CODES = {10003, 50001, 50013}


# ---------------------------------------------------------------------------
# Errors: messages carry status and Discord error codes only, never the token.
# ---------------------------------------------------------------------------
class DiscordSourceError(RuntimeError):
    """Base class of the errors this source raises."""


class DiscordAuthError(DiscordSourceError):
    """Discord rejected the bot token (HTTP 401)."""


class DiscordIntentError(DiscordSourceError):
    """The app does not have the Message Content Intent enabled."""


class DiscordAPIError(DiscordSourceError):
    """Discord answered with an error, or could not be reached."""

    def __init__(self, message: str, status: int | None = None, code: int | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


# ---------------------------------------------------------------------------
# Snowflakes
# ---------------------------------------------------------------------------
def snowflake_time(snowflake: str | int) -> datetime:
    milliseconds = (int(snowflake) >> 22) + DISCORD_EPOCH_MS
    return datetime.fromtimestamp(milliseconds / 1000, tz=UTC)


def time_snowflake(moment: datetime) -> int:
    """The smallest snowflake Discord can assign at ``moment``."""
    return max(int(moment.timestamp() * 1000) - DISCORD_EPOCH_MS, 0) << 22


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
def _json(response: Any) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class DiscordClient:
    """Minimal synchronous REST client for a Discord bot.

    ``get`` returns the decoded JSON body. Tests and hosts can pass any object
    with the same method to :func:`discord_source` instead.
    """

    def __init__(
        self, token: str, *, http: Any = None, sleep: Callable[[float], None] = time.sleep
    ):
        token = (token or "").strip().removeprefix("Bot ").strip()
        # Reject early and without echoing the value: httpx would put an invalid
        # header value, token included, into its exception.
        if not token or not token.isascii() or not token.isprintable() or " " in token:
            raise ValueError("The Discord bot token is empty or has an invalid format")
        import httpx

        self._headers = {"Authorization": f"Bot {token}", "User-Agent": USER_AGENT}
        self._http = http or httpx.Client(timeout=_TIMEOUT)
        self._sleep = sleep

    def __repr__(self) -> str:
        return "DiscordClient(token=<redacted>)"

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        import httpx

        for attempt in range(_RETRIES):
            last = attempt == _RETRIES - 1
            try:
                response = self._http.get(API_URL + path, params=params, headers=self._headers)
            except httpx.TransportError as exc:
                if last:
                    raise DiscordAPIError(f"Discord request failed: {type(exc).__name__}") from None
                self._sleep(float(2**attempt))
                continue
            status = response.status_code
            if status == 429 and not last:
                body = _json(response)
                wait = _float(body.get("retry_after") if isinstance(body, dict) else None)
                wait = wait if wait is not None else _float(response.headers.get("Retry-After"))
                self._sleep(min(wait if wait is not None else 2.0**attempt, _MAX_RETRY_SLEEP))
                continue
            if status >= 500 and not last:
                self._sleep(float(2**attempt))
                continue
            if status == 401:
                raise DiscordAuthError("Discord rejected the bot token (HTTP 401)")
            if status != 200:
                body = _json(response)
                code = body.get("code") if isinstance(body, dict) else None
                code = code if isinstance(code, int) else None
                detail = f" (code {code})" if code is not None else ""
                raise DiscordAPIError(
                    f"Discord request failed: HTTP {status}{detail}", status=status, code=code
                )
            # Wait out an exhausted bucket instead of earning a 429; Discord bans
            # an IP for too many 401/403/429 responses.
            if response.headers.get("X-RateLimit-Remaining") == "0":
                reset = _float(response.headers.get("X-RateLimit-Reset-After")) or 0.0
                self._sleep(min(reset, _MAX_RETRY_SLEEP))
            return response.json()
        raise DiscordAPIError("Discord request failed")  # pragma: no cover


def _is_gone(error: DiscordAPIError) -> bool:
    return error.status in (403, 404) and error.code in _GONE_CODES


# ---------------------------------------------------------------------------
# Rendering: deterministic, no volatile fields.
# ---------------------------------------------------------------------------
_USER_MENTION = re.compile(r"<@!?(\d+)>")
_ROLE_MENTION = re.compile(r"<@&(\d+)>")
_CHANNEL_MENTION = re.compile(r"<#(\d+)>")
_CUSTOM_EMOJI = re.compile(r"<a?:(\w+):\d+>")


def _user_name(user: Any) -> str:
    if not isinstance(user, dict):
        return "Unknown"
    return str(user.get("global_name") or user.get("username") or "Unknown")


@dataclass(frozen=True)
class _Names:
    channels: dict[str, str]
    roles: dict[str, str]


def _message_text(message: dict, names: _Names) -> str:
    users = {str(u.get("id")): _user_name(u) for u in message.get("mentions") or []}
    text = str(message.get("content") or "")
    text = _USER_MENTION.sub(lambda m: "@" + users.get(m.group(1), "unknown-user"), text)
    text = _ROLE_MENTION.sub(lambda m: "@" + names.roles.get(m.group(1), "unknown-role"), text)
    text = _CHANNEL_MENTION.sub(
        lambda m: "#" + names.channels.get(m.group(1), "unknown-channel"), text
    )
    text = _CUSTOM_EMOJI.sub(lambda m: f":{m.group(1)}:", text)
    # Attachment URLs are signed and expire, so only the file name is kept.
    files = [
        f"[attachment: {a['filename']}]"
        for a in message.get("attachments") or []
        if a.get("filename")
    ]
    return " ".join(part for part in [text.strip(), *files] if part)


def render_message(message: dict, names: _Names) -> str | None:
    """Render one message as ``[HH:MM] Author: text``, or None when it has no text."""
    stamp = snowflake_time(message["id"]).strftime("%H:%M")
    source = message
    if message.get("type") == _THREAD_STARTER:
        # A thread started from a message carries that message as its reference.
        source = message.get("referenced_message") or {}
    text = _message_text(source, names) if source else ""
    if not text:
        return None
    author = _user_name(source.get("author"))
    reference = message.get("referenced_message")
    if message.get("type") == 19 and isinstance(reference, dict):
        author = f"{author} (reply to {_user_name(reference.get('author'))})"
    return f"[{stamp}] {author}: {text}"


def render_day(
    container: dict, day: date, lines: list[str], first_id: str, guild: dict
) -> dict[str, Any]:
    name = container["name"]
    if container["kind"] == "channel":
        header = [f"Channel: #{name}"]
        title = f"#{name} {day.isoformat()}"
    else:
        label = "Forum post" if container["kind"] == "forum_post" else "Thread"
        header = [f"{label}: {name} (in #{container['parent']})"]
        if container.get("tags"):
            header.append(f"Tags: {', '.join(container['tags'])}")
        title = f"{name} {day.isoformat()}"
    header.extend([f"Server: {guild['name']}", f"Date: {day.isoformat()}"])
    return {
        "id": f"discord:{container['id']}:{day.isoformat()}",
        "title": title,
        "content": "\n".join([*header, "", *lines]),
        "url": f"https://discord.com/channels/{guild['id']}/{container['id']}/{first_id}",
        "_deleted": False,
    }


def _row_hash(row: dict) -> str:
    payload = [row["title"], row["content"], row["url"]]
    return hashlib.md5(json.dumps(payload).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _DiscordConfig:
    guild_id: str
    channel_ids: tuple[str, ...]
    include_threads: bool
    include_forums: bool
    include_private_threads: bool
    include_bot_messages: bool
    since_days: int
    rescan_days: int
    full_resync: bool


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _check_intent(client: Any) -> None:
    flags = int((client.get("/applications/@me") or {}).get("flags") or 0)
    if not flags & (_GATEWAY_MESSAGE_CONTENT | _GATEWAY_MESSAGE_CONTENT_LIMITED):
        raise DiscordIntentError(
            "The bot does not have the Message Content Intent, so Discord would return "
            "empty messages. Enable it under Bot > Privileged Gateway Intents in the "
            "Developer Portal."
        )


def _archived_threads(client: Any, channel_id: str, private: bool) -> Iterator[dict]:
    kind = "private" if private else "public"
    path = f"/channels/{channel_id}/threads/archived/{kind}"
    before = None
    while True:
        params: dict[str, Any] = {"limit": _PAGE_SIZE}
        if before:
            params["before"] = before
        page = client.get(path, params)
        threads = page.get("threads") or []
        yield from threads
        if not page.get("has_more") or not threads:
            return
        # Archived threads page by archive time, not by snowflake.
        next_before = (threads[-1].get("thread_metadata") or {}).get("archive_timestamp")
        if not next_before or next_before == before:
            raise DiscordAPIError("Discord thread pagination did not advance")
        before = next_before


def _messages_after(client: Any, channel_id: str, after: int) -> list[dict]:
    messages: list[dict] = []
    cursor = after
    while True:
        page = client.get(
            f"/channels/{channel_id}/messages", {"after": str(cursor), "limit": _PAGE_SIZE}
        )
        if not page:
            return messages
        messages.extend(page)
        # Pages come newest first, so the next page starts after the highest id.
        newest = max(int(m["id"]) for m in page)
        if len(page) < _PAGE_SIZE:
            return messages
        if newest <= cursor:
            raise DiscordAPIError("Discord message pagination did not advance")
        cursor = newest


def _containers(client: Any, config: _DiscordConfig, channels: list[dict]) -> dict[str, dict]:
    """Every channel and thread whose messages are synced, keyed by id."""
    wanted = set(config.channel_ids)
    selected = {
        str(c["id"]): c
        for c in channels
        if c.get("type") in _TEXT_CHANNEL_TYPES | _FORUM_CHANNEL_TYPES
        and (not wanted or str(c["id"]) in wanted)
    }
    containers = {
        cid: {
            "id": cid,
            "kind": "channel",
            "name": c.get("name") or cid,
            "last_message_id": c.get("last_message_id"),
        }
        for cid, c in selected.items()
        if c.get("type") in _TEXT_CHANNEL_TYPES
    }
    if not (config.include_threads or config.include_forums):
        return containers

    active = client.get(f"/guilds/{config.guild_id}/threads/active") or {}
    threads = list(active.get("threads") or [])
    for cid, channel in selected.items():
        threads.extend(_archived_threads(client, cid, private=False))
        if config.include_private_threads and channel.get("type") == 0:
            threads.extend(_archived_threads(client, cid, private=True))

    for thread in threads:
        parent = selected.get(str(thread.get("parent_id")))
        if parent is None:
            continue
        if thread.get("type") == _PRIVATE_THREAD and not config.include_private_threads:
            continue
        in_forum = parent.get("type") in _FORUM_CHANNEL_TYPES
        if (in_forum and not config.include_forums) or (
            not in_forum and not config.include_threads
        ):
            continue
        tag_names = {str(t["id"]): t.get("name") for t in parent.get("available_tags") or []}
        applied = [tag_names.get(str(t)) for t in thread.get("applied_tags") or []]
        tags = [tag for tag in applied if tag]
        tid = str(thread["id"])
        containers[tid] = {
            "id": tid,
            "kind": "forum_post" if in_forum else "thread",
            "name": thread.get("name") or tid,
            "parent": parent.get("name") or str(parent["id"]),
            "tags": sorted(tags),
            "last_message_id": thread.get("last_message_id"),
        }
    return containers


def _wanted(message: dict, config: _DiscordConfig) -> bool:
    if message.get("type") not in _MESSAGE_TYPES:
        return False
    author = message.get("author") or {}
    is_bot = bool(author.get("bot") or message.get("webhook_id"))
    return config.include_bot_messages or not is_bot


def _blank(message: dict) -> bool:
    """A user message with no content at all, as Discord sends it without the intent."""
    keys = ("content", "attachments", "embeds", "sticker_items", "poll")
    return message.get("type") == 0 and not any(message.get(key) for key in keys)


def _iter_rows(
    client: Any,
    config: _DiscordConfig,
    state: dict,
    stats: dict[str, int],
    *,
    now: Callable[[], datetime] | None = None,
) -> Iterator[dict]:
    """Yield changed day documents and tombstones. Pure of dlt, so tests drive it with a dict."""
    today = (now or _utcnow)().date()
    since = today - timedelta(days=config.since_days)
    rescan_floor = today - timedelta(days=config.rescan_days)

    _check_intent(client)
    guild = client.get(f"/guilds/{config.guild_id}")
    guild = {"id": str(guild["id"]), "name": guild.get("name") or str(guild["id"])}
    roles = {
        str(r["id"]): r.get("name") or "role"
        for r in client.get(f"/guilds/{config.guild_id}/roles")
    }
    channels = client.get(f"/guilds/{config.guild_id}/channels")
    containers = _containers(client, config, channels)
    channel_names = {str(c["id"]): c.get("name") or str(c["id"]) for c in channels}
    channel_names.update({cid: c["name"] for cid, c in containers.items()})
    names = _Names(channels=channel_names, roles=roles)

    known: dict[str, dict] = state.setdefault("containers", {})
    seen: set[str] = set()
    for cid in sorted(containers):
        container = containers[cid]
        entry = known.get(cid)
        if entry is None or config.full_resync:
            start = since
        else:
            start = min(snowflake_time(entry["cursor"]).date(), rescan_floor)
        # Start at midnight so the first day is re-read whole, never in part.
        after = time_snowflake(datetime(start.year, start.month, start.day, tzinfo=UTC)) - 1
        try:
            messages = _messages_after(client, cid, after)
        except DiscordAPIError as exc:
            if not _is_gone(exc):
                raise
            stats["gone"] += 1
            continue
        seen.add(cid)

        days: dict[str, str] = dict(entry["days"]) if entry else {}
        last_id = container.get("last_message_id")
        if not messages and days and last_id and int(last_id) > after:
            # Messages exist but none came back: without Read Message History
            # Discord answers with an empty list, which must not look like deletion.
            logger.warning("Discord: no readable history in channel %s, left unchanged.", cid)
            stats["unreadable"] += 1
            continue

        by_day: dict[str, list[dict]] = {}
        for message in messages:
            by_day.setdefault(snowflake_time(message["id"]).date().isoformat(), []).append(message)

        for day in sorted(set(by_day) | {d for d in days if d >= start.isoformat()}):
            ordered = sorted(by_day.get(day, []), key=lambda m: int(m["id"]))
            rendered = [(m, render_message(m, names)) for m in ordered if _wanted(m, config)]
            lines = [line for _, line in rendered if line]
            stats["empty"] += sum(1 for m, line in rendered if not line and _blank(m))
            if not lines:
                if days.pop(day, None) is not None:
                    stats["deleted"] += 1
                    yield {"id": f"discord:{cid}:{day}", "_deleted": True}
                continue
            first_id = next(m["id"] for m, line in rendered if line)
            row = render_day(container, date.fromisoformat(day), lines, first_id, guild)
            digest = _row_hash(row)
            if days.get(day) == digest:
                stats["unchanged"] += 1
                continue
            days[day] = digest
            stats["emitted"] += 1
            yield row

        newest = max((int(m["id"]) for m in messages), default=0)
        cursor = max(newest, int(entry["cursor"]) if entry else after)
        known[cid] = {"cursor": str(cursor), "days": days}

    # Deleted, deselected, unknown or no longer visible: forget everything it held.
    for cid in [cid for cid in known if cid not in seen]:
        for day in known.pop(cid)["days"]:
            stats["deleted"] += 1
            yield {"id": f"discord:{cid}:{day}", "_deleted": True}

    if stats["empty"] and not stats["emitted"] and not stats["unchanged"]:
        logger.warning(
            "Discord: messages came back without content. Check the Message Content Intent."
        )
    logger.info(
        "Discord: %d day document(s) emitted, %d deleted, %d channel(s) gone.",
        stats["emitted"],
        stats["deleted"],
        stats["gone"],
    )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def discord_source(
    guild_id: str | None = None,
    token: str | None = None,
    *,
    channel_ids: list[str] | None = None,
    include_threads: bool = True,
    include_forums: bool = True,
    include_private_threads: bool = False,
    include_bot_messages: bool = False,
    since_days: int = DEFAULT_SINCE_DAYS,
    rescan_days: int = DEFAULT_RESCAN_DAYS,
    full_resync: bool = False,
    resource_name: str = "discord_messages",
    check_active: Callable[[], None] | None = None,
    client: Any = None,
):
    """Return a ``dlt`` resource that yields one document per channel or thread per day.

    Hand the result to ``cognee.remember(...)`` with ``write_disposition="merge"``
    and ``primary_key="id"``.

    Args:
        guild_id: The server id (``DISCORD_GUILD_ID``).
        token: Bot token (``DISCORD_BOT_TOKEN``).
        channel_ids: Channel ids to sync. Defaults to every text, announcement,
            forum and media channel the bot can read.
        include_threads: Sync active and archived public threads of text channels.
        include_forums: Sync forum and media posts.
        include_private_threads: Also sync private threads. Archived ones need the
            Manage Threads permission.
        include_bot_messages: Keep messages from bots and webhooks.
        since_days: How far back the first sync of a channel reads.
        rescan_days: Recent days re-read on every run to pick up edits and
            deletions. 0 reads only new messages.
        full_resync: Re-read the whole ``since_days`` history once.
        resource_name: Staging table name. Use a different one per sync scope
            that shares a dataset.
        check_active: Optional host authorization checkpoint, called around each row.
        client: Pre-built client (see :class:`DiscordClient`). Mainly an injection
            point for tests and hosts.

    Returns:
        A ``dlt`` resource configured with ``primary_key="id"``,
        ``write_disposition="merge"`` and a ``_deleted`` hard-delete column.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError('The Discord connector requires dlt: pip install "cognee[dlt]".') from exc

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError(
            "Discord sync requires a cognee build with table-scoped DLT document cleanup."
        )

    resolved_guild = guild_id or os.getenv("DISCORD_GUILD_ID")
    if not resolved_guild:
        raise ValueError("guild_id is required (pass it explicitly or set DISCORD_GUILD_ID).")
    if client is None:
        resolved_token = token or os.getenv("DISCORD_BOT_TOKEN")
        if not resolved_token:
            raise ValueError("A bot token is required (pass token= or set DISCORD_BOT_TOKEN).")
        client = DiscordClient(resolved_token)

    config = _DiscordConfig(
        guild_id=str(resolved_guild),
        channel_ids=tuple(str(c) for c in channel_ids or ()),
        include_threads=include_threads,
        include_forums=include_forums,
        include_private_threads=include_private_threads,
        include_bot_messages=include_bot_messages,
        since_days=int(since_days),
        rescan_days=max(int(rescan_days), 0),
        full_resync=full_resync,
    )
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def discord_messages():
        stats.clear()
        stats.update(emitted=0, unchanged=0, deleted=0, gone=0, unreadable=0, empty=0)
        rows = _iter_rows(client, config, dlt.current.resource_state(), stats)
        yield from dlt_utils.guarded_rows(rows, check_active)

    resource = discord_messages()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "discord")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    resource.cognee_sync_stats = stats
    return resource
