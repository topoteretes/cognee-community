"""Twitch connector for cognee: a ``dlt`` source that turns the channels a user
follows, and their recent VODs and clips, into memory.

Hand the source to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_twitch import twitch_source

    await cognee.remember(
        twitch_source(client_id="...", access_token="..."),
        dataset_name="twitch",
        primary_key="id",
        write_disposition="merge",   # REQUIRED, see below
        max_rows_per_table=0,        # compare forget-on-delete against every row
    )

Design
------
* **Auth**: a user access token with the ``user:read:follows`` scope plus the
  Client-ID of the app it was issued to (the README has the walkthrough). The
  token is validated once, when the source is created (``GET /users``), so a
  bad token fails before any sync work. The token is only ever sent to
  ``api.twitch.tv`` and never logged, never in a repr and never in an error:
  every raise uses ``from None`` so the HTTP error carrying the request never
  follows it.
* **Full re-list + diff**: Helix has no events feed a user token can read, so
  every run re-lists the follows (paginated) and each selected channel's VODs
  and clips (paginated), then diffs against the keys stored in dlt state. A
  channel in state but not in the follows listing was unfollowed: the channel
  row and every stored VOD/clip row of it is emitted with the ``_deleted``
  hard-delete marker, and cognee's ``orphan_cleanup`` forgets them. A VOD or
  clip in state but not in the current listing expired or was removed: its row
  is deleted the same way.
* **Deterministic rows**: rows are rendered from API fields only, no fetch
  timestamps, so an unchanged item keeps its content hash and is not
  re-cognified. ``view_count`` is deliberately excluded from the rendered
  content: it changes with every view, so keeping it would re-cognify every
  VOD and clip on every run. The trade-off (no view counts in memory) is
  documented in the README.
* **Node sets**: every row carries ``cognee_node_set: ["channel:<login>"]``, so
  a channel's documents link in the graph without LLM extraction.
* **Rate limits**: Helix answers with ``Ratelimit-Remaining`` points and a
  ``Ratelimit-Reset`` epoch. When remaining points run low the client waits
  until the reset before the next request; 429s are retried the same way. A
  wait longer than a minute is not slept through: the run stops cleanly
  (``failed_rate_limit`` in ``cognee_sync_stats``), no state moves and the
  next run resumes. Rows are built before the first one is yielded, so a run
  that stops this way emits nothing at all.
* **State ownership**: the sync state is scoped to ``twitch:<login>`` (a
  chosen ``resource_name`` scopes by name, telegram-style) and records the
  user id; a state written by another user's token is refused instead of
  mixing two users' rows into one table.
* **Selection**: ``channels=[login, ...]`` syncs only some follows, and
  ``max_channels`` (default 25) bounds a run. Follows are listed most recently
  followed first, so the bound keeps the newest follows. A channel that stays
  followed but leaves the selection keeps its rows in memory; only an
  unfollow forgets them.

.. important::
   ``write_disposition="merge"`` is mandatory. The add pipeline defaults to
   ``"replace"``, which would wipe every document from earlier syncs on the
   second run.

Helix limits
------------
* No live-chat history: reading chat needs IRC or EventSub, out of scope.
* Past-broadcast VODs expire on Twitch after a retention window, so they
  vanish from the listing and are forgotten here too.
* The clips endpoint returns a broadcaster's top clips (descending by view
  count, roughly 1,000 across all pages), so a clip that falls out of the top
  listing is forgotten here even if it still exists on Twitch.
"""

from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timezone
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import (
    DOCUMENT_SOURCE_ATTR,
    NODE_SET_COLUMN,
    NODE_SET_COLUMN_HINT,
    PIPELINE_SCOPE_ATTR,
    guarded_rows,
)

logger = get_logger("twitch_connector")

TWITCH_SOURCE_NAME = "twitch"

_API_BASE = "https://api.twitch.tv/helix"
_MAX_RETRIES = 5
_PAGE_SIZE = 100  # Helix maximum for first= on every endpoint used here
_MAX_RATE_WAIT = 60.0  # longer waits stop the run instead of sleeping
# Back off once this few points remain in the window; a run uses about 60 points.
_RATE_LIMIT_BACKOFF_AT = 25
_DEFAULT_MAX_CHANNELS = 25
_TITLE_LENGTH = 80
# dlt snake-cases table names, so "Rust-Channel" and "rust_channel" would share a
# table while keeping separate sync states. Only names dlt leaves unchanged are
# accepted; a login is always safe.
_RESOURCE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_DURATION_RE = re.compile(r"^(?:PT)?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", re.IGNORECASE)


class TwitchAPIError(RuntimeError):
    """A Helix call failed permanently."""


class TwitchAuthError(TwitchAPIError):
    """Twitch rejected the access token (or the Client-ID does not match it)."""


class TwitchRateLimitError(TwitchAPIError):
    """Twitch's rate limit asks to wait longer than the connector sleeps."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


# ---------------------------------------------------------------------------
# Minimal Helix client (stdlib only)
# ---------------------------------------------------------------------------
class TwitchClient:
    """Small Helix client: ``get(path, **params) -> payload dict``.

    List parameters (``broadcaster_id=[...]``, ``id=[...]``) are sent as
    repeated query parameters, which is how Helix batches ids. Retries 429
    (honouring the rate-limit reset, capped at a minute) and 5xx/network
    errors with backoff; 401/403 raise :class:`TwitchAuthError` immediately.
    The access token is a password: it never appears in a repr, a log or an
    error (``from None`` keeps the HTTPError, which knows the request, out of
    the traceback).
    """

    def __init__(
        self,
        client_id: str,
        access_token: str,
        timeout: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._client_id = client_id
        self._access_token = access_token
        self._timeout = timeout
        self._sleep = sleep
        self._remaining: int | None = None
        self._reset_at: int | None = None

    def __repr__(self) -> str:
        # The access token is a password: keep it out of any repr, log or traceback.
        return f"TwitchClient(client_id={self._client_id!r}, access_token=<redacted>)"

    def _open(self, request):
        return urllib.request.urlopen(request, timeout=self._timeout)

    def _headers(self) -> dict[str, str]:
        return {
            "Client-Id": self._client_id,
            "Authorization": f"Bearer {self._access_token}",
        }

    def get(self, path: str, **params: Any) -> dict:
        """GET ``{path}`` with the query parameters and return the JSON payload."""
        pairs: list[tuple[str, Any]] = []
        for key, value in params.items():
            if value is None:
                continue
            values = value if isinstance(value, (list, tuple)) else [value]
            pairs.extend((key, item) for item in values)
        url = f"{_API_BASE}/{path.lstrip('/')}"
        if pairs:
            url = f"{url}?{urllib.parse.urlencode(pairs)}"
        for attempt in range(_MAX_RETRIES):
            self._wait_if_low()
            request = urllib.request.Request(url, headers=self._headers())
            try:
                with self._open(request) as response:
                    self._note_rate_limit(getattr(response, "headers", None))
                    payload = json.load(response)
            except urllib.error.HTTPError as exc:
                status = exc.code
                if status in (401, 403):
                    raise TwitchAuthError(
                        "Twitch rejected the access token (or the Client-ID does not match "
                        "it). Get a fresh user token as the README describes and pass it as "
                        "access_token=."
                    ) from None
                wait = self._reset_wait(_error_headers(exc))
                if status == 429:
                    if wait is not None and wait > _MAX_RATE_WAIT:
                        raise TwitchRateLimitError(
                            f"Twitch {path}: rate-limit wait of {wait:.0f}s.", wait
                        ) from None
                    if attempt == _MAX_RETRIES - 1:
                        raise TwitchRateLimitError(
                            f"Twitch {path}: still rate limited after {_MAX_RETRIES} tries.",
                            wait,
                        ) from None
                if (status == 429 or status >= 500) and attempt < _MAX_RETRIES - 1:
                    delay = wait if (status == 429 and wait is not None) else float(2**attempt)
                    logger.warning(
                        "Twitch %s: HTTP %s, retrying in %.1fs (%d/%d).",
                        path,
                        status,
                        delay,
                        attempt + 1,
                        _MAX_RETRIES,
                    )
                    self._sleep(delay)
                    continue
                raise TwitchAPIError(f"Twitch {path} failed: HTTP {status}") from None
            except ValueError:
                # A 200 whose body is not JSON (a proxy's error page, a cut-off reply).
                # ``from None``: the decode error keeps the whole body.
                raise TwitchAPIError(f"Twitch {path} returned a body that is not JSON.") from None
            except (OSError, http.client.HTTPException) as exc:
                # URLError, timeouts, ConnectionResetError, RemoteDisconnected and
                # IncompleteRead: a dropped connection is retried like a 5xx.
                if attempt < _MAX_RETRIES - 1:
                    delay = float(2**attempt)
                    logger.warning("Twitch %s: network error, retrying in %.1fs.", path, delay)
                    self._sleep(delay)
                    continue
                # ``from None``: the error's text can include the URL.
                reason = getattr(exc, "reason", exc)
                raise TwitchAPIError(f"Twitch {path} failed: {type(reason).__name__}") from None
            return payload
        raise TwitchAPIError(f"Twitch {path} failed after {_MAX_RETRIES} attempts.")

    def _note_rate_limit(self, headers) -> None:
        if headers is None:
            return
        remaining = _header_int(headers, "Ratelimit-Remaining")
        if remaining is not None:
            self._remaining = remaining
            self._reset_at = _header_int(headers, "Ratelimit-Reset")

    def _wait_if_low(self) -> None:
        """Sleep until the window resets before spending its last points."""
        if self._remaining is None or self._remaining > _RATE_LIMIT_BACKOFF_AT:
            return
        remaining, self._remaining = self._remaining, None
        wait = self._reset_wait()
        if wait is None:
            return
        if wait > _MAX_RATE_WAIT:
            raise TwitchRateLimitError(
                f"Twitch: only {remaining} rate-limit point(s) left and the window resets in "
                f"{wait:.0f}s.",
                wait,
            )
        logger.info(
            "Twitch: %d rate-limit point(s) left; waiting %.1fs for the reset.", remaining, wait
        )
        self._sleep(wait)

    def _reset_wait(self, headers=None) -> float | None:
        reset = _header_int(headers, "Ratelimit-Reset") if headers is not None else self._reset_at
        if reset is None:
            return None
        return max(0.0, reset - time.time())


class _CheckedClient:
    """Runs the host's ``check_active`` before every Helix call."""

    def __init__(self, client, check_active: Callable[[], None]):
        self._client = client
        self._check_active = check_active

    def __repr__(self) -> str:
        return f"_CheckedClient({self._client!r})"

    def get(self, path: str, **params: Any) -> dict:
        self._check_active()
        return self._client.get(path, **params)


def _error_headers(exc: urllib.error.HTTPError):
    # ``hdrs`` is always set; newer Pythons also expose ``headers``.
    return getattr(exc, "hdrs", None) or getattr(exc, "headers", None)


def _header_int(headers, name: str) -> int | None:
    try:
        value = headers.get(name)
    except AttributeError:
        return None
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def channel_key(broadcaster_id: str) -> str:
    return f"channel:{broadcaster_id}"


def video_key(video_id: str) -> str:
    return f"video:{video_id}"


def clip_key(clip_id: str) -> str:
    return f"clip:{clip_id}"


def parse_duration(duration: str | None) -> str:
    """``PT2H14M33S`` (or Helix's unprefixed ``2h14m33s``) as ``2h 14m 33s``.

    Zero components are dropped (``3m21s`` stays ``3m 21s``); anything the
    pattern does not match comes back empty rather than wrong.
    """
    if not duration:
        return ""
    match = _DURATION_RE.match(str(duration).strip())
    if not match:
        return ""
    parts = [
        f"{int(value)}{unit}"
        for value, unit in zip(match.groups(), ("h", "m", "s"), strict=False)
        if value
    ]
    return " ".join(parts)


def _field(label: str, value: Any) -> str:
    return f"{label}: {value}" if value not in (None, "") else ""


def _title(text: str | None, fallback: str) -> str:
    text = (text or "").strip()
    if not text:
        return fallback
    return text if len(text) <= _TITLE_LENGTH else text[: _TITLE_LENGTH - 1].rstrip() + "…"


def _fmt_timestamp(value: Any) -> str:
    """An RFC3339 timestamp as ``YYYY-MM-DD HH:MM UTC`` (unparseable values pass through)."""
    if not value:
        return ""
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def channel_to_row(
    follow: dict,
    info: dict | None,
    description: str | None,
    video_count: int,
    clip_count: int,
) -> dict[str, Any] | None:
    """Render a followed channel as a document row, or ``None`` if it has no id/login.

    ``follow`` is a ``channels/followed`` item, ``info`` the channel's
    ``GET /channels`` payload (current category) and ``description`` the
    channel description from ``GET /users`` — that endpoint owns it; the
    channels endpoint does not return one. The row is deterministic (no fetch
    timestamps) so an unchanged channel keeps its content hash.
    """
    broadcaster_id = follow.get("broadcaster_id")
    info = info or {}
    login = follow.get("broadcaster_login") or info.get("broadcaster_login")
    if broadcaster_id is None or not login:
        return None
    name = follow.get("broadcaster_name") or info.get("broadcaster_name") or login
    lines = [
        f"Twitch channel {name} ({login}).",
        _field("Description", description),
        _field("Current category", info.get("game_name")),
        _field("Followed", _fmt_timestamp(follow.get("followed_at"))),
        f"Ingested: {video_count} VOD(s), {clip_count} clip(s).",
    ]
    return {
        "id": channel_key(broadcaster_id),
        "title": _title(name, f"Channel {broadcaster_id}"),
        "content": "\n".join(line for line in lines if line),
        "url": f"https://www.twitch.tv/{login}",
        NODE_SET_COLUMN: [f"channel:{login}"],
        "_deleted": False,
    }


def video_to_row(video: dict) -> dict[str, Any] | None:
    """Render a ``GET /videos`` item as a document row, or ``None`` without id or login.

    ``view_count`` is intentionally not rendered (it changes with every view
    and would break the content hash); the README documents the trade-off.
    Helix returns no category on a video, so none is invented here.
    """
    video_id = video.get("id")
    login = video.get("user_login")
    if video_id is None or not login:
        return None
    name = video.get("user_name") or login
    kind = {"archive": "past broadcast", "highlight": "highlight reel", "upload": "upload"}.get(
        video.get("type"), video.get("type")
    )
    lines = [
        f"Twitch VOD \"{video.get('title') or ''}\" by {name} ({login}).",
        _field("Type", kind),
        _field("Description", video.get("description")),
        _field("Created", _fmt_timestamp(video.get("created_at"))),
        _field("Published", _fmt_timestamp(video.get("published_at"))),
        _field("Duration", parse_duration(video.get("duration"))),
    ]
    return {
        "id": video_key(video_id),
        "title": _title(video.get("title"), f"VOD {video_id}"),
        "content": "\n".join(line for line in lines if line),
        "url": video.get("url"),
        NODE_SET_COLUMN: [f"channel:{login}"],
        "_deleted": False,
    }


def clip_to_row(clip: dict, login: str | None) -> dict[str, Any] | None:
    """Render a ``GET /clips`` item as a document row (``None`` without an id).

    Clips name no login of their own, so the channel's login (from the follows
    listing) is passed in and used for the row's node set. As with VODs,
    ``view_count`` is deliberately not rendered.
    """
    clip_id = clip.get("id")
    if clip_id is None:
        return None
    name = clip.get("broadcaster_name") or ""
    channel = f"{name} ({login})" if login and name else (login or name)
    title = clip.get("title") or ""
    if channel:
        first = f"Twitch clip \"{title}\" from channel {channel}."
    else:
        first = f"Twitch clip \"{title}\"."
    lines = [
        first,
        _field("Created by", clip.get("creator_name")),
        _field("Created", _fmt_timestamp(clip.get("created_at"))),
    ]
    return {
        "id": clip_key(clip_id),
        "title": _title(title, f"Clip {clip_id}"),
        "content": "\n".join(line for line in lines if line),
        "url": clip.get("url"),
        NODE_SET_COLUMN: [f"channel:{login}"] if login else [],
        "_deleted": False,
    }


def deleted_row(key: str) -> dict[str, Any]:
    """Hard-delete marker for a stored row."""
    return {"id": key, "_deleted": True}


# ---------------------------------------------------------------------------
# Listing (Helix pagination)
# ---------------------------------------------------------------------------
def _paginate(client, path: str, **params: Any) -> Iterator[dict]:
    """Walk a Helix list endpoint to its end: pages of ``first=100`` via the ``after`` cursor."""
    after = None
    while True:
        payload = client.get(path, first=_PAGE_SIZE, after=after, **params)
        data = payload.get("data") or []
        yield from data
        after = (payload.get("pagination") or {}).get("cursor")
        if not after or not data:
            return


def _batches(ids: Iterable[str], size: int = _PAGE_SIZE) -> Iterator[list[str]]:
    items = [item for item in ids if item is not None]
    for start in range(0, len(items), size):
        yield items[start : start + size]


def list_follows(client, user_id: str) -> list[dict]:
    """Every channel the user follows (``GET /channels/followed``), most recently followed first."""
    return list(_paginate(client, "channels/followed", user_id=user_id))


def get_channel_info(client, broadcaster_ids: Iterable[str]) -> dict[str, dict]:
    """``GET /channels`` batched over repeated ``broadcaster_id`` params: id -> payload."""
    out: dict[str, dict] = {}
    for batch in _batches(broadcaster_ids):
        for item in client.get("channels", broadcaster_id=batch).get("data") or []:
            if item.get("broadcaster_id") is not None:
                out[item["broadcaster_id"]] = item
    return out


def get_channel_descriptions(client, broadcaster_ids: Iterable[str]) -> dict[str, str]:
    """Channel descriptions via ``GET /users`` batched over repeated ``id`` params.

    The channels endpoint does not return a description; the user object does
    ("the user's description of their channel").
    """
    out: dict[str, str] = {}
    for batch in _batches(broadcaster_ids):
        for item in client.get("users", id=batch).get("data") or []:
            if item.get("id") is not None:
                out[item["id"]] = item.get("description") or ""
    return out


def list_videos(client, broadcaster_id: str) -> list[dict]:
    """A channel's videos (``GET /videos?user_id=...&type=all``), newest first."""
    return list(_paginate(client, "videos", user_id=broadcaster_id, type="all"))


def list_clips(client, broadcaster_id: str) -> list[dict]:
    """A channel's clips (``GET /clips?broadcaster_id=...``), most viewed first."""
    return list(_paginate(client, "clips", broadcaster_id=broadcaster_id))


# ---------------------------------------------------------------------------
# Sync (pure given a client + state dict — unit-testable)
# ---------------------------------------------------------------------------
def sync_channels(
    client,
    state: dict,
    *,
    user: dict,
    channels: Iterable[str] | None = None,
    max_channels: int = _DEFAULT_MAX_CHANNELS,
    stats: dict | None = None,
) -> Iterator[dict[str, Any]]:
    """Turn one full re-list of follows, VODs and clips into document rows.

    ``state`` (dlt resource state) holds the followed ``channels`` stored so
    far (broadcaster id -> login) and the row ``items`` stored per channel.
    The run works on copies and writes them back only after its last row, and
    dlt only persists the state after the load succeeds, so a failed or
    stopped run replays in full next time. A rate-limit wait beyond a minute
    ends the run before a single row: ``stats`` reports
    ``failed_rate_limit`` and nothing is lost.
    """
    if stats is None:
        stats = {}
    stats.clear()
    stats.update(items=0, deletions=0, skipped=0, failed=0)
    selected_logins = None
    if channels is not None:
        selected_logins = {str(name).strip().lower() for name in channels if str(name).strip()}
    try:
        follows = list_follows(client, str(user["id"]))
        followed_ids = {f.get("broadcaster_id") for f in follows}
        selection = follows
        if selected_logins is not None:
            known_logins = {str(f.get("broadcaster_login") or "").lower() for f in follows}
            missing = selected_logins - known_logins
            if missing:
                logger.warning(
                    "Twitch: not followed (or misspelled), left unsynced: %s.",
                    ", ".join(sorted(missing)),
                )
            selection = [
                follow
                for follow in follows
                if str(follow.get("broadcaster_login") or "").lower() in selected_logins
            ]
        if len(selection) > max_channels:
            logger.warning(
                "Twitch: %d follow(s) beyond max_channels=%d left for a later run; follows are "
                "listed most recently followed first, so the newest ones are kept here.",
                len(selection) - max_channels,
                max_channels,
            )
            selection = selection[:max_channels]
        stats["skipped"] += len(follows) - len(selection)

        known_channels: dict[str, str] = dict(state.get("channels", {}))
        known_items: dict[str, list[str]] = {k: list(v) for k, v in state.get("items", {}).items()}
        rows: list[dict[str, Any]] = []

        # An unfollowed channel: the channel row and all of its stored content go.
        for broadcaster_id in sorted(set(known_channels) - followed_ids):
            login = known_channels.pop(broadcaster_id)
            rows.append(deleted_row(channel_key(broadcaster_id)))
            for key in sorted(known_items.pop(broadcaster_id, [])):
                rows.append(deleted_row(key))
            logger.info(
                "Twitch: unfollowed %s; forgetting the channel and its VODs and clips.",
                login or broadcaster_id,
            )

        infos = get_channel_info(client, (follow.get("broadcaster_id") for follow in selection))
        descriptions = get_channel_descriptions(
            client, (follow.get("broadcaster_id") for follow in selection)
        )

        for follow in selection:
            broadcaster_id = follow.get("broadcaster_id")
            login = follow.get("broadcaster_login") or (infos.get(broadcaster_id) or {}).get(
                "broadcaster_login"
            )
            videos = [
                row for row in (video_to_row(v) for v in list_videos(client, broadcaster_id)) if row
            ]
            clips = [
                row for row in (clip_to_row(c, login) for c in list_clips(client, broadcaster_id))
                if row
            ]
            row = channel_to_row(
                follow,
                infos.get(broadcaster_id),
                descriptions.get(broadcaster_id),
                len(videos),
                len(clips),
            )
            if row is None:
                stats["skipped"] += 1
                continue
            rows.append(row)
            rows.extend(videos)
            rows.extend(clips)
            # An expired or removed VOD/clip: gone from the listing, so forgotten.
            current = [item["id"] for item in videos] + [item["id"] for item in clips]
            for key in sorted(set(known_items.get(broadcaster_id, [])) - set(current)):
                rows.append(deleted_row(key))
            known_channels[broadcaster_id] = login or ""
            known_items[broadcaster_id] = current
    except TwitchAuthError:
        stats.update(failed=1, failed_auth=1)
        raise
    except TwitchRateLimitError as exc:
        # Stop cleanly: no row is out yet and no state has moved, so the next
        # run re-lists everything. Raising would only fail the host's run.
        stats.update(failed=1, failed_rate_limit=1)
        logger.warning(
            "Twitch: rate limited%s; stopping this run before any row, the next sync resumes.",
            f" for {exc.retry_after:.0f}s" if exc.retry_after else "",
        )
        return

    stats["items"] += sum(1 for row in rows if not row["_deleted"])
    stats["deletions"] += sum(1 for row in rows if row["_deleted"])
    logger.info(
        "Twitch: %d row(s) written, %d forgotten, %d channel(s) skipped.",
        stats["items"],
        stats["deletions"],
        stats["skipped"],
    )
    yield from rows

    # Only reached once every row is out: a run stopped mid-way saves nothing.
    state["user_id"] = str(user["id"])
    state["channels"] = known_channels
    state["items"] = known_items


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def twitch_source(
    client_id: str | None = None,
    access_token: str | None = None,
    channels: Iterable[str] | None = None,
    max_channels: int = _DEFAULT_MAX_CHANNELS,
    resource_name: str | None = None,
    check_active: Callable[[], None] | None = None,
    client: Any = None,
    service: Any = None,
):
    """Create a dlt source that yields followed channels, their VODs and clips as documents.

    Args:
        client_id: The Client-ID of the app registered at dev.twitch.tv/console.
        access_token: A user access token with the ``user:read:follows`` scope
            (the README has the walkthrough). Validated once, here, so a bad
            token fails before any sync work.
        channels: Logins of follows to sync, e.g. ``["ruststreams"]``. ``None``
            syncs every follow up to ``max_channels``. A channel that stays
            followed but leaves the selection keeps its rows in memory; only
            an unfollow forgets them.
        max_channels: How many channels one run syncs, 1 to 100. Follows are
            listed most recently followed first, so a bound keeps the newest
            follows; raise it (or use ``channels=``) to reach the rest.
        resource_name: Table name and part of the sync state key. Defaults to
            ``twitch_<login>``. Keep it fixed across runs.
        check_active: Optional host authorization checkpoint, called before every
            Helix call and around every row (``dlt_utils.guarded_rows``);
            raising from it stops the sync.
        client: Object with ``get(path, **params)``; a test injection point.
            Built from the credentials when omitted. ``service`` is an alias.

    Returns:
        A dlt source with one ``<resource_name>`` resource
        (``primary_key="id"``, ``write_disposition="merge"``, ``_deleted`` hard
        delete, ``cognee_node_set`` node sets). Pass ``write_disposition="merge"``
        to ``cognee.remember`` too. ``source.cognee_sync_stats`` holds counts
        only: ``items``, ``deletions``, ``skipped``, ``failed``,
        ``failed_auth`` and ``failed_rate_limit``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Twitch connector requires dlt: pip install "dlt[sqlalchemy]".'
        ) from exc

    if client is not None and service is not None and client is not service:
        raise ValueError("Pass client= or service=, not both.")
    client = client if client is not None else service
    client_id = (client_id or "").strip() or None
    access_token = (access_token or "").strip() or None
    channels = [str(name) for name in channels] if channels is not None else None
    if not isinstance(max_channels, int) or max_channels < 1:
        raise ValueError("max_channels must be a positive integer.")
    if resource_name and not _RESOURCE_NAME_RE.match(resource_name):
        raise ValueError(
            "resource_name must be lowercase letters, digits and underscores, starting with "
            "a letter, so dlt uses it unchanged as the table name."
        )
    if client is None:
        if not client_id or not access_token:
            raise ValueError(
                "Twitch needs a Client-ID and a user access token: register an app at "
                "https://dev.twitch.tv/console/apps, then get a user token with the "
                "user:read:follows scope (see this connector's README) and pass them as "
                "client_id= and access_token=."
            )
        client = TwitchClient(client_id, access_token)
    if check_active is not None:
        client = _CheckedClient(client, check_active)
    stats: dict[str, int] = {}

    # Fail fast: one GET /users validates the token and identifies the user
    # whose follows this source is about.
    user = authenticated_user(client)
    login = str(user.get("login") or user.get("id"))
    default_name = f"twitch_{login}"
    resource_name = resource_name or default_name

    def _check_owner(state: dict) -> None:
        owner, current = state.get("user_id"), str(user.get("id") or "")
        if owner is not None and owner != current:
            raise ValueError(
                f"This sync state ('{resource_name}') belongs to another Twitch user. Give "
                "each user their own dataset or resource_name."
            )
        state["user_id"] = current

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns={
            "_deleted": {"data_type": "bool", "hard_delete": True},
            # Typed up front so a column that is empty in a run (``url``, the
            # text columns in a deletion-only run) still exists.
            "title": {"data_type": "text", "nullable": True},
            "content": {"data_type": "text", "nullable": True},
            "url": {"data_type": "text", "nullable": True},
            **NODE_SET_COLUMN_HINT,
        },
    )
    def twitch_rows():
        # The caller's write_disposition overrides the one declared above. A
        # replace run of this source's diff would wipe every table but the
        # rows of this run, so refuse it before reading anything.
        disposition = source.resources[resource_name].write_disposition
        if disposition != "merge":
            raise ValueError(
                f'twitch_source needs write_disposition="merge", got {disposition!r}. '
                "Its runs emit only the current listing plus deletions."
            )
        state = dlt.current.resource_state()
        _check_owner(state)
        yield from guarded_rows(
            sync_channels(
                client,
                state,
                user=user,
                channels=channels,
                max_channels=max_channels,
                stats=stats,
            ),
            check_active,
        )

    @dlt.source(name=TWITCH_SOURCE_NAME)
    def _twitch():
        return twitch_rows

    source = _twitch()
    # Route rows through the document path (text -> cognify), like Notion.
    setattr(source, DOCUMENT_SOURCE_ATTR, TWITCH_SOURCE_NAME)
    # One dlt pipeline, and so one sync state, per user by default, and per
    # name when one is chosen, so two users given one name share one state and
    # the second is refused instead of silently writing into one table.
    scope = f"twitch:{login}" if resource_name == default_name else f"twitch:{resource_name}"
    setattr(source, PIPELINE_SCOPE_ATTR, scope)
    # Host-readable diagnostics contain counts only, never channel names or text.
    source.cognee_sync_stats = stats
    source.resources[resource_name].cognee_sync_stats = stats
    return source


def authenticated_user(client) -> dict:
    """``GET /users`` with the token: the authenticated user's payload.

    Raises :class:`TwitchAuthError` for a rejected token, and also when Helix
    answers with no user (a token of the wrong type, say an app token).
    """
    data = client.get("users").get("data") or []
    if not data:
        raise TwitchAuthError(
            "Twitch returned no user for this token. It must be a user access token with the "
            "user:read:follows scope; the README describes how to get one."
        )
    return data[0]
