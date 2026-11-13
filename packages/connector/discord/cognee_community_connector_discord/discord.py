"""Discord connector for cognee: a ``dlt`` source that turns a server's conversations into memory.

Sync a Discord server's channel messages, threads and forum posts into cognee,
incrementally and with forget-on-delete. The resource is handed directly to
:func:`cognee.remember`::

    import cognee
    from cognee_community_connector_discord import discord_source

    await cognee.remember(
        discord_source(guild_id="<server id>", token=token),
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
  day of ``min(cursor, now - rescan_days)``, never before ``since_days``, so
  edits and deletions inside that window are caught. A channel or thread with
  nothing new and nothing inside the window costs no request. Older days are
  left as they are; ``full_resync`` re-reads everything.
* **Forget-on-delete.** A day whose messages were all deleted, a deleted thread
  or forum post, and a channel that is deleted or deselected are emitted as
  hard-delete tombstones. A channel the bot cannot read is left as it is.
* **A run is bounded.** On a long rate limit or a spent request budget it stops
  cleanly, keeps the days it finished, and reports why in ``cognee_sync_stats``.
  Deleted channels and threads are decided right after discovery, which always
  completes, so a run cut short by the budget still forgets them.

Privacy
-------
This reads every message the bot can see. Nothing is fetched until you construct
a source and call ``remember``. Give the bot a role limited to the channels you
want in memory, and remember that anyone with read access to the target dataset
can read what was ingested.
"""

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("discord_connector")

API_URL = "https://discord.com/api/v10"
# Discord requires this format and may block requests without it.
USER_AGENT = "DiscordBot (https://github.com/topoteretes/cognee-community, 0.1.0)"
DISCORD_EPOCH_MS = 1420070400000
# The first sync of a channel reads about one quarter back.
DEFAULT_SINCE_DAYS = 90
# Edits and deletions are caught for a week behind the cursor.
DEFAULT_RESCAN_DAYS = 7
# 40 seconds at Discord's global limit of 50 requests per second.
DEFAULT_MAX_REQUESTS = 2000
# Largest ``limit`` the messages and archived threads endpoints accept.
_PAGE_SIZE = 100
# Discord usually answers in well under a second; only a stuck connection hits this.
_TIMEOUT = 30.0
# Attempts per request; backing off 1, 2, 4 and 8 seconds gives up after about 15.
_RETRIES = 5
# Route limits reset within seconds; a longer retry_after is the global limit,
# which stops the run instead of blocking it.
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
_REFUSED_CODES = {10003, 50001, 50013}
_STATS = (
    "scanned",
    "emitted",
    "unchanged",
    "deleted",
    "unreadable",
    "empty",
    "skipped_no_text",
    "failed",
)


# ---------------------------------------------------------------------------
# Errors: messages carry status and Discord error codes only, never the token.
# ---------------------------------------------------------------------------
class DiscordSourceError(RuntimeError):
    """Base class of the errors this source raises."""


class DiscordAuthError(DiscordSourceError):
    """Discord rejected the bot token (HTTP 401)."""


class DiscordIntentError(DiscordSourceError):
    """The app does not have the Message Content Intent enabled."""


class DiscordRateLimitedError(DiscordSourceError):
    """Discord kept answering HTTP 429, or asked for a longer wait than the client allows."""


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
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)


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
        self,
        token: str,
        *,
        http: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        check_active: Callable[[], None] | None = None,
    ):
        token = (token or "").strip().removeprefix("Bot ").strip()
        # Reject early and without echoing the value: httpx would put an invalid
        # header value, token included, into its exception.
        if not token or not token.isascii() or not token.isprintable() or " " in token:
            raise ValueError("The Discord bot token is empty or has an invalid format")

        self._headers = {"Authorization": f"Bot {token}", "User-Agent": USER_AGENT}
        # An injected client belongs to the caller; one made here is closed by close().
        self._http = http
        self._owned = http is None
        self._sleep = sleep
        self._check_active = check_active

    def __repr__(self) -> str:
        return "DiscordClient(token=<redacted>)"

    def close(self) -> None:
        """Close the HTTP client this object made; the next request opens a new one."""
        if self._owned and self._http is not None:
            self._http.close()
            self._http = None

    def _wait(self, seconds: float) -> None:
        # The host may revoke access while a request waits, so ask before and after.
        if self._check_active is not None:
            self._check_active()
        self._sleep(seconds)
        if self._check_active is not None:
            self._check_active()

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        import httpx

        if self._http is None:
            self._http = httpx.Client(timeout=_TIMEOUT)
        for attempt in range(_RETRIES):
            last = attempt == _RETRIES - 1
            try:
                response = self._http.get(API_URL + path, params=params, headers=self._headers)
            except httpx.TransportError as exc:
                if last:
                    raise DiscordAPIError(f"Discord request failed: {type(exc).__name__}") from exc
                self._wait(float(2**attempt))
                continue
            status = response.status_code
            if status == 429:
                body = _json(response)
                wait = _float(body.get("retry_after") if isinstance(body, dict) else None)
                wait = wait if wait is not None else _float(response.headers.get("Retry-After"))
                if last or (wait is not None and wait > _MAX_RETRY_SLEEP):
                    raise DiscordRateLimitedError("Discord rate limit exceeded (HTTP 429)")
                self._wait(wait if wait is not None else float(2**attempt))
                continue
            if status >= 500 and not last:
                self._wait(float(2**attempt))
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
                self._wait(min(reset, _MAX_RETRY_SLEEP))
            return response.json()
        raise DiscordAPIError("Discord request failed")  # pragma: no cover


def _refused(error: DiscordAPIError) -> bool:
    return error.status in (403, 404) and error.code in _REFUSED_CODES


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


# Bots fill required embed fields with zero-width characters, which strip() keeps.
_FILLER = dict.fromkeys(map(ord, "\u200b\u2060\ufeff"))
# ZWNJ and ZWJ join emoji and Persian or Indic words, so they stay in real text.
_JOINERS = "\u200c\u200d"


def _clean(value: Any) -> str:
    text = str(value or "").translate(_FILLER).strip()
    return text if text.strip(_JOINERS).strip() else ""


def _extras(message: dict) -> list[str]:
    """Embeds, stickers and polls, which webhooks and bots often send without text."""
    parts = []
    for embed in message.get("embeds") or []:
        # Link previews only repeat a URL that is already in the text.
        if embed.get("type", "rich") != "rich":
            continue
        text = ": ".join(
            p for p in (_clean(embed.get("title")), _clean(embed.get("description"))) if p
        )
        text = " ".join(p for p in (text, _clean(embed.get("url"))) if p)
        # Bots and CI webhooks often put everything in fields, the author and the footer.
        fields = "; ".join(
            ": ".join(p for p in (_clean(f.get("name")), _clean(f.get("value"))) if p)
            for f in embed.get("fields") or []
            if _clean(f.get("name")) or _clean(f.get("value"))
        )
        author = _clean((embed.get("author") or {}).get("name"))
        footer = _clean((embed.get("footer") or {}).get("text"))
        segments = " | ".join(p for p in (author, text, fields, footer) if p)
        if segments:
            parts.append(f"[embed: {segments}]")
    parts.extend(
        f"[sticker: {s['name']}]" for s in message.get("sticker_items") or [] if s.get("name")
    )
    poll = message.get("poll") or {}
    question = _clean((poll.get("question") or {}).get("text"))
    if question:
        answers = [
            _clean((a.get("poll_media") or {}).get("text")) for a in poll.get("answers") or []
        ]
        choices = " / ".join(a for a in answers if a)
        parts.append(f"[poll: {question}" + (f" ({choices})]" if choices else "]"))
    return parts


def _message_text(message: dict, names: _Names) -> str:
    users = {str(u.get("id")): _user_name(u) for u in message.get("mentions") or []}
    text = _clean(message.get("content"))
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
    return " ".join(part for part in [text.strip(), *_extras(message), *files] if part)


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
    max_requests: int = DEFAULT_MAX_REQUESTS


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def _day_of(message: dict) -> str:
    return snowflake_time(message["id"]).date().isoformat()


def _next_day(messages: list[dict]) -> str:
    return (date.fromisoformat(_day_of(messages[-1])) + timedelta(days=1)).isoformat()


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


def _messages_after(client: Any, channel_id: str, after: int) -> Iterator[dict]:
    """Messages after ``after``, oldest first, holding one page at a time."""
    cursor = after
    while True:
        page = client.get(
            f"/channels/{channel_id}/messages", {"after": str(cursor), "limit": _PAGE_SIZE}
        )
        if not page:
            return
        # Pages come newest first, so the next page starts after the highest id.
        page = sorted(page, key=lambda m: int(m["id"]))
        yield from page
        if len(page) < _PAGE_SIZE:
            return
        newest = int(page[-1]["id"])
        if newest <= cursor:
            raise DiscordAPIError("Discord message pagination did not advance")
        cursor = newest


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


class _CutShortError(Exception):
    """Internal: stop the run here, keeping the days finished so far."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Walker:
    """One run over a server: discovery, then each channel and thread, then tombstones."""

    def __init__(
        self,
        client: Any,
        config: _DiscordConfig,
        state: dict,
        stats: dict[str, int],
        check_active: Callable[[], None] | None,
        today: date,
    ):
        self.client = client
        self.config = config
        self.state = state
        self.stats = stats
        self.check_active = check_active
        self.since = today - timedelta(days=config.since_days)
        # The re-scan never reaches further back than the first sync read.
        self.rescan_floor = max(today - timedelta(days=config.rescan_days), self.since)
        self.known: dict[str, dict] = state.setdefault("containers", {})
        self.requests = 0
        # Set by a new day or a container that finished syncing; the budget never stops a
        # run before that.
        self.progressed = False
        # A hidden channel refuses both its thread list and its messages; count it once.
        self.unreadable: set[str] = set()
        # While a full_resync runs: the containers it finished and how far it got in
        # the others, so a resync cut short by the budget resumes instead of restarting.
        self.resync: dict | None = None
        if config.full_resync:
            resync = state.get("resync")
            if not resync or resync.get("since_days") != config.since_days:
                # A changed window would leave the finished containers on the old one.
                resync = state["resync"] = {
                    "since_days": config.since_days,
                    "done": [],
                    "cursors": {},
                }
            self.resync = resync
        else:
            # A resync given up part way must not shorten a later one.
            state.pop("resync", None)
        self.names = _Names(channels={}, roles={})
        self.guild: dict = {}

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if self.check_active is not None:
            self.check_active()
        if self.progressed and self.requests >= self.config.max_requests:
            raise _CutShortError("budget")
        self.requests += 1
        try:
            return self.client.get(path, params)
        except DiscordRateLimitedError:
            raise _CutShortError("rate_limit") from None

    def _refuse(self, cid: str, reason: str) -> None:
        """Leave a channel or thread list the bot cannot read as it is."""
        if cid in self.unreadable:
            return
        self.unreadable.add(cid)
        logger.warning("Discord: channel %s cannot be read (%s), left unchanged.", cid, reason)
        self.stats["unreadable"] += 1

    # -- discovery ---------------------------------------------------------
    def _discover(self) -> tuple[dict[str, dict], set[str], set[str]]:
        """Every channel and thread to sync, the channels whose threads could not be
        listed, and the channels whose archived private threads could not be listed."""
        flags = int((self.get("/applications/@me") or {}).get("flags") or 0)
        if not flags & (_GATEWAY_MESSAGE_CONTENT | _GATEWAY_MESSAGE_CONTENT_LIMITED):
            raise DiscordIntentError(
                "The bot does not have the Message Content Intent, so Discord would return "
                "empty messages. Enable it under Bot > Privileged Gateway Intents in the "
                "Developer Portal."
            )
        config = self.config
        guild = self.get(f"/guilds/{config.guild_id}")
        self.guild = {"id": str(guild["id"]), "name": guild.get("name") or str(guild["id"])}
        roles = {
            str(r["id"]): r.get("name") or "role"
            for r in self.get(f"/guilds/{config.guild_id}/roles")
        }
        channels = self.get(f"/guilds/{config.guild_id}/channels")
        # A 200 that is not a list must not read as "every channel was deleted".
        if not isinstance(channels, list):
            raise DiscordAPIError("Discord answered without a channel list")

        wanted = set(config.channel_ids)
        selected = {
            str(c["id"]): c
            for c in channels
            if c.get("type") in _TEXT_CHANNEL_TYPES | _FORUM_CHANNEL_TYPES
            and (not wanted or str(c["id"]) in wanted)
        }
        missing = wanted - set(selected)
        if missing:
            raise DiscordSourceError(
                f"Discord channel(s) {', '.join(sorted(missing))} in channel_ids are not text, "
                "announcement, forum or media channels of this server"
            )
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
        hidden: set[str] = set()
        hidden_private: set[str] = set()
        if config.include_threads or config.include_forums:
            containers.update(self._threads(selected, hidden, hidden_private))

        names = {str(c["id"]): c.get("name") or str(c["id"]) for c in channels}
        names.update({cid: c["name"] for cid, c in containers.items()})
        self.names = _Names(channels=names, roles=roles)
        return containers, hidden, hidden_private

    def _threads(
        self, selected: dict[str, dict], hidden: set[str], hidden_private: set[str]
    ) -> dict[str, dict]:
        config = self.config
        active = self.get(f"/guilds/{config.guild_id}/threads/active")
        # Nor one without its thread list "every active thread was deleted".
        if not isinstance(active, dict) or not isinstance(active.get("threads"), list):
            raise DiscordAPIError("Discord answered without an active thread list")
        threads = list(active["threads"])
        for cid, channel in selected.items():
            try:
                threads.extend(_archived_threads(self, cid, private=False))
            except DiscordAPIError as exc:
                if not _refused(exc):
                    raise
                self._refuse(cid, f"code {exc.code}")
                hidden.add(cid)
                continue
            if not (config.include_private_threads and channel.get("type") == 0):
                continue
            try:
                threads.extend(_archived_threads(self, cid, private=True))
            except DiscordAPIError as exc:
                if not _refused(exc):
                    raise
                # Listing archived private threads needs Manage Threads; the channel
                # itself reads fine, so only its private threads are kept as they are.
                logger.warning(
                    "Discord: archived private threads of channel %s cannot be listed "
                    "(code %s), left unchanged.",
                    cid,
                    exc.code,
                )
                hidden_private.add(cid)

        containers = {}
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
            tid = str(thread["id"])
            containers[tid] = {
                "id": tid,
                "kind": "forum_post" if in_forum else "thread",
                "name": thread.get("name") or tid,
                "parent": parent.get("name") or str(parent["id"]),
                "parent_id": str(parent["id"]),
                "tags": sorted(tag for tag in applied if tag),
                "last_message_id": thread.get("last_message_id"),
                "private": thread.get("type") == _PRIVATE_THREAD,
            }
        return containers

    # -- one channel or thread ---------------------------------------------
    def _save(
        self, container: dict, cursor: int, days: dict[str, str], resyncing: bool = False
    ) -> None:
        previous = self.known.get(container["id"])
        if resyncing and self.resync is not None:
            # A resync reads from the start again: remember how far it got, and keep
            # the incremental cursor where it was.
            self.resync["cursors"][container["id"]] = str(cursor)
            if previous:
                cursor = max(cursor, int(previous["cursor"]))
        # A run re-reads from min(cursor day, rescan_floor) and rescan_floor is never
        # before since, so older days are never compared again: only their key is
        # kept, for forgetting.
        old = min(self.since, snowflake_time(cursor).date()).isoformat()
        entry: dict[str, Any] = {
            "cursor": str(cursor),
            "days": {day: (digest if day >= old else "") for day, digest in days.items()},
        }
        if container.get("parent_id"):
            entry["parent"] = container["parent_id"]
            entry["private"] = bool(container.get("private"))
        self.known[container["id"]] = entry

    def _container(self, container: dict, resyncing: bool) -> Iterator[dict]:
        """Yield one channel's or thread's changed days, saving its state as days finish.

        Returns whether the container was synced, so a container left unread is not
        counted as done or as progress.
        """
        cid = container["id"]
        entry = self.known.get(cid)
        days: dict[str, str] = dict(entry["days"]) if entry else {}
        resumed = self.resync["cursors"].get(cid) if resyncing and entry else None
        if resumed:
            # A resync that a budget cut short goes on from where it stopped.
            cursor = int(resumed)
            start = snowflake_time(cursor).date()
        elif entry is None or resyncing:
            start = self.since
            cursor = 0
        else:
            cursor = int(entry["cursor"])
            start = min(snowflake_time(cursor).date(), self.rescan_floor)
            last_id = container.get("last_message_id")
            if start < self.rescan_floor and last_id and int(last_id) <= cursor:
                # Nothing new and nothing inside the re-scan window: no request needed.
                self._save(container, cursor, days)
                return True
        after = time_snowflake(_midnight(start)) - 1
        # Never behind the start day, so an empty channel does not re-read the day before.
        cursor = max(cursor, after + 1)
        # Only a day past where this run began counts as progress for the budget.
        begin = cursor

        # Days from ``low`` on that come back without messages were emptied upstream.
        low = start.isoformat()
        batch: list[dict] = []
        for message in _messages_after(self, cid, after):
            self.stats["scanned"] += 1
            if batch and _day_of(message) != _day_of(batch[0]):
                yield from self._day(container, batch, days, low)
                low, cursor = _next_day(batch), max(cursor, int(batch[-1]["id"]))
                self._save(container, cursor, days, resyncing)
                self.progressed = self.progressed or cursor > begin
                batch = []
            batch.append(message)
        if batch:
            yield from self._day(container, batch, days, low)
            low, cursor = _next_day(batch), max(cursor, int(batch[-1]["id"]))
        elif any(d >= low for d in days) and self._history_hidden(container):
            self._refuse(cid, "no readable history")
            return False
        yield from self._missing_days(cid, days, low, None)
        self._save(container, cursor, days, resyncing)
        return True

    def _history_hidden(self, container: dict) -> bool:
        """Whether an empty answer means missing Read Message History, not deletion.

        Without that permission Discord answers with an empty list. A channel that
        has messages (``last_message_id`` can point at a deleted one) and still
        returns no latest message is treated as unreadable.
        """
        if not container.get("last_message_id"):
            return False
        return not self.get(f"/channels/{container['id']}/messages", {"limit": 1})

    def _day(
        self, container: dict, messages: list[dict], days: dict[str, str], low: str
    ) -> Iterator[dict]:
        cid, day = container["id"], _day_of(messages[0])
        yield from self._missing_days(cid, days, low, day)
        rendered = [(m, render_message(m, self.names)) for m in messages if _wanted(m, self.config)]
        lines = [line for _, line in rendered if line]
        for message, line in rendered:
            if not line:
                self.stats["empty" if _blank(message) else "skipped_no_text"] += 1
        if not lines:
            if days.pop(day, None) is not None:
                self.stats["deleted"] += 1
                yield {"id": f"discord:{cid}:{day}", "_deleted": True}
            return
        first_id = next(m["id"] for m, line in rendered if line)
        row = render_day(container, date.fromisoformat(day), lines, first_id, self.guild)
        digest = _row_hash(row)
        if days.get(day) == digest:
            self.stats["unchanged"] += 1
            return
        days[day] = digest
        self.stats["emitted"] += 1
        yield row

    def _missing_days(
        self, cid: str, days: dict[str, str], low: str, high: str | None
    ) -> Iterator[dict]:
        for day in sorted(d for d in days if d >= low and (high is None or d < high)):
            del days[day]
            self.stats["deleted"] += 1
            yield {"id": f"discord:{cid}:{day}", "_deleted": True}

    # -- the run -----------------------------------------------------------
    def rows(self) -> Iterator[dict]:
        containers, hidden, hidden_private = self._discover()
        # Discovery always finishes before the budget can stop a run, so deleted and
        # deselected containers are decided here, even when the walk is cut short.
        yield from self._forget(set(containers), hidden, hidden_private)
        # Start after the container a cut-short run reached, so every one gets its turn.
        order = sorted(containers)
        resume = self.state.get("resume")
        if resume:
            order = [c for c in order if c > resume] + [c for c in order if c <= resume]
        for cid in order:
            # A container this resync already finished still gets its new messages.
            resyncing = self.resync is not None and cid not in self.resync["done"]
            try:
                synced = yield from self._container(containers[cid], resyncing)
            except DiscordAPIError as exc:
                if not _refused(exc):
                    raise
                if cid in self.config.channel_ids:
                    raise DiscordSourceError(
                        f"Discord channel {cid} in channel_ids cannot be read (code {exc.code})"
                    ) from exc
                self._refuse(cid, f"code {exc.code}")
                synced = False
            self.state["resume"] = cid
            if not synced:
                # Left unread: neither done for the resync nor progress for the budget.
                continue
            if resyncing:
                self.resync["done"].append(cid)
                self.resync["cursors"].pop(cid, None)
            self.progressed = True
        self.state.pop("resume", None)
        if self.resync is not None:
            self.state.pop("resync", None)

    def _forget(
        self, discovered: set[str], hidden: set[str], hidden_private: set[str]
    ) -> Iterator[dict]:
        """Deleted or deselected channels and threads. Threads of a channel whose
        thread list could not be read, and private threads of a channel whose archived
        private list could not be read, are kept."""
        for cid in [cid for cid in self.known if cid not in discovered]:
            entry = self.known[cid]
            parent = entry.get("parent")
            # State saved before the flag existed has no "private" key: keep those too.
            if parent in hidden or (entry.get("private", True) and parent in hidden_private):
                continue
            if self.resync is not None:
                self.resync["cursors"].pop(cid, None)
            for day in sorted(self.known.pop(cid)["days"]):
                self.stats["deleted"] += 1
                yield {"id": f"discord:{cid}:{day}", "_deleted": True}


def _iter_rows(
    client: Any,
    config: _DiscordConfig,
    state: dict,
    stats: dict[str, int],
    *,
    now: Callable[[], datetime] | None = None,
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict]:
    """Yield changed day documents and tombstones. Pure of dlt, so tests drive it with a dict."""
    for key in _STATS:
        stats.setdefault(key, 0)
    walker = _Walker(client, config, state, stats, check_active, (now or _utcnow)().date())
    try:
        yield from walker.rows()
    except _CutShortError as cut:
        # A spent budget is a planned pause, not a failure; failed_budget says why. The
        # budget only stops a run after a new day was saved or a container finished
        # syncing (a container left unread does not count), so a run that cannot
        # advance never ends on it.
        if cut.reason != "budget":
            stats["failed"] = 1
        stats[f"failed_{cut.reason}"] = 1
        logger.warning("Discord sync stopped early (%s); it resumes on the next run.", cut.reason)
        return
    if stats["empty"] and not stats["emitted"] and not stats["unchanged"]:
        logger.warning(
            "Discord: messages came back without content. Check the Message Content Intent."
        )
    logger.info(
        "Discord: %d day document(s) emitted, %d deleted, %d channel(s) unreadable.",
        stats["emitted"],
        stats["deleted"],
        stats["unreadable"],
    )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
# dlt table names stay readable up to this length; the merged Asana connector uses it too.
_MAX_RESOURCE_NAME = 64


def _check_resource_name(name: Any) -> None:
    """A non-empty name of at most 64 characters that dlt keeps as it is.

    It names the table and scopes the state: an empty name would lose that scope,
    and two names dlt normalizes alike ("Foo-Bar", "foo_bar") would share a table.
    """
    from dlt.common.normalizers.naming.snake_case import NamingConvention

    if not isinstance(name, str) or not name:
        raise ValueError("resource_name must be a non-empty string")
    if len(name) > _MAX_RESOURCE_NAME:
        raise ValueError(f"resource_name must be at most {_MAX_RESOURCE_NAME} characters")
    if NamingConvention().normalize_table_identifier(name) != name:
        raise ValueError("resource_name must be lowercase snake_case (letters, digits, _)")


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
    max_requests: int = DEFAULT_MAX_REQUESTS,
    resource_name: str | None = None,
    check_active: Callable[[], None] | None = None,
    service: Any = None,
    client: Any = None,
):
    """Return a ``dlt`` resource that yields one document per channel or thread per day.

    Hand the result to ``cognee.remember(...)`` with ``write_disposition="merge"``
    and ``primary_key="id"``.

    Args:
        guild_id: The server id. Required; the factory never reads the environment.
        token: Bot token. Required unless ``service`` is given.
        channel_ids: Channel ids to sync. A listed channel that is missing or cannot
            be read raises. Defaults to every text, announcement, forum and media
            channel of the server.
        include_threads: Sync active and archived public threads of text channels.
        include_forums: Sync forum and media posts.
        include_private_threads: Also sync private threads. Archived ones need the
            Manage Threads permission; without it the known private threads are kept.
        include_bot_messages: Keep messages from bots and webhooks.
        since_days: How far back the first sync of a channel reads.
        rescan_days: Recent days re-read on every run to pick up edits and
            deletions, never further back than ``since_days``. With 0 only the
            cursor's day and newer messages are read.
        full_resync: Re-read the whole ``since_days`` history once. A resync cut short
            by the budget resumes on the next run with the flag still set.
        max_requests: Request budget of one run, checked before each request once a
            new day or a whole channel is done. A bigger server finishes over several
            runs; a spent budget sets ``failed_budget`` but not ``failed``.
        resource_name: Staging table name, which scopes this sync's state and cleanup.
            Defaults to ``discord_<guild id>``. Use a different one per channel
            selection of one server that shares a dataset. A name you pass must be
            lowercase snake_case of at most 64 characters.
        check_active: Optional host authorization checkpoint, called before each
            request and around each row, and in the client built here around every
            retry or rate-limit wait.
        service: Pre-built client (see :class:`DiscordClient`). Mainly an injection
            point for tests and hosts. ``client`` is accepted as an alias.

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

    # Credentials and the server come from the caller only; the example reads the
    # environment, the factory never does.
    if not guild_id or not str(guild_id).strip():
        raise ValueError("guild_id is required: pass the server id explicitly.")
    guild = str(guild_id).strip()
    # One table and state per server by default, so two servers in one dataset never
    # forget each other's channels.
    name = resource_name
    if name is None:
        # Guild ids are numeric; anything else keeps a valid name through its hash.
        slug = re.sub(r"[^a-z0-9]+", "_", guild.lower()).strip("_")
        if slug != guild:
            digest = hashlib.sha256(guild.encode()).hexdigest()[:8]
            slug = f"{slug}_{digest}" if slug else digest
        name = "discord_" + slug
    _check_resource_name(name)
    service = service or client
    owned = service is None
    if owned:
        if not token:
            raise ValueError("A bot token is required: pass token= or a pre-built service.")
        service = DiscordClient(token, check_active=check_active)

    config = _DiscordConfig(
        guild_id=guild,
        channel_ids=tuple(str(c) for c in channel_ids or ()),
        include_threads=include_threads,
        include_forums=include_forums,
        include_private_threads=include_private_threads,
        include_bot_messages=include_bot_messages,
        since_days=int(since_days),
        rescan_days=max(int(rescan_days), 0),
        full_resync=full_resync,
        max_requests=int(max_requests),
    )
    stats: dict[str, int] = {}

    @dlt.resource(
        name=name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def discord_messages():
        stats.clear()
        state = dlt.current.resource_state()
        rows = _iter_rows(service, config, state, stats, check_active=check_active)
        try:
            yield from dlt_utils.guarded_rows(rows, check_active)
        finally:
            # Also on errors and when dlt stops early; an injected service is the caller's.
            if owned:
                service.close()

    resource = discord_messages()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "discord")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, name)
    resource.cognee_sync_stats = stats
    return resource
