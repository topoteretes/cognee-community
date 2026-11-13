"""Incremental Helix metadata ingestion with confirmed hard deletes."""

import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx


class TwitchAPIError(RuntimeError):
    """An API request failed; state is left unchanged for the next sync."""


class TwitchAuthError(TwitchAPIError):
    """The user token expired, is invalid, or lacks user:read:follows."""


class TwitchRateLimitError(TwitchAPIError):
    """The bounded retry/wait budget was exhausted. Retry the sync later."""


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _duration(value: str) -> int:
    """Helix uses 1h2m3s; also accept ISO-8601 PT1H2M3S."""
    match = re.fullmatch(r"(?:PT)?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value.upper())
    if not match or not any(match.groups()):
        raise TwitchAPIError("Twitch returned an invalid VOD duration.")
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


class _API:
    def __init__(
        self,
        client: httpx.Client,
        client_id: str,
        token: str,
        check_active: Callable[[], None] | None = None,
    ) -> None:
        self.client = client
        self.headers = {"Client-Id": client_id, "Authorization": f"Bearer {token}"}
        self.check_active = check_active
        self.wait_left = 120.0
        self.reset_at = 0.0

    def wait(self, seconds: float) -> None:
        if not math.isfinite(seconds) or seconds > 60 or seconds > self.wait_left:
            raise TwitchRateLimitError("Twitch rate limit wait exceeded; retry the sync later.")
        seconds = max(0.0, seconds)
        self.wait_left -= seconds
        while seconds > 0:
            if self.check_active:
                self.check_active()
            step = min(1.0, seconds)
            time.sleep(step)
            seconds -= step

    def get(self, endpoint: str, params: Any = None) -> list[dict[str, Any]]:
        return self.page(endpoint, params)["data"]

    def page(
        self,
        endpoint: str,
        params: Any = None,
        *,
        missing_videos_ok: bool = False,
    ) -> dict[str, Any]:
        for attempt in range(4):
            self.wait(max(0.0, self.reset_at - time.time()))
            self.reset_at = 0.0
            if self.check_active:
                self.check_active()
            try:
                response = self.client.get(
                    f"https://api.twitch.tv/helix/{endpoint}",
                    params=params,
                    headers=self.headers,
                    timeout=30,
                )
            except httpx.RequestError:
                # Never include request headers, token, or provider error bodies.
                raise TwitchAPIError("Twitch network request failed; retry the sync.") from None
            if response.status_code in (401, 403):
                raise TwitchAuthError(
                    "Twitch authorization failed. Supply a fresh user access token with "
                    "user:read:follows and its matching Client ID."
                )
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 3:
                    error = TwitchRateLimitError if response.status_code == 429 else TwitchAPIError
                    raise error("Twitch request retries exhausted; retry the sync later.")
                try:
                    delay = max(
                        float(response.headers.get("Retry-After", 2**attempt)),
                        float(response.headers.get("Ratelimit-Reset", 0)) - time.time(),
                    )
                except ValueError:
                    raise TwitchAPIError("Twitch returned invalid rate-limit headers.") from None
                self.wait(delay)
                continue
            # Helix returns 404 when *all* video ids in an explicit lookup are gone.
            if response.status_code == 404 and missing_videos_ok:
                return {"data": []}
            if response.status_code != 200:
                raise TwitchAPIError(f"Twitch request failed (HTTP {response.status_code}).")
            if response.headers.get("Ratelimit-Remaining") == "0":
                try:
                    self.reset_at = float(response.headers.get("Ratelimit-Reset", time.time() + 1))
                except ValueError:
                    raise TwitchAPIError("Twitch returned an invalid rate-limit reset.") from None
            try:
                body = response.json()
                if not isinstance(body, dict) or not isinstance(body.get("data"), list):
                    raise ValueError
                if not all(isinstance(item, dict) for item in body["data"]):
                    raise ValueError
                return body
            except ValueError:
                raise TwitchAPIError("Twitch returned an invalid response.") from None
        raise AssertionError("unreachable")

    def pages(self, endpoint: str, **params: Any) -> Iterator[dict[str, Any]]:
        seen: set[str] = set()
        while True:
            body = self.page(endpoint, {"first": 100, **params})
            yield from body["data"]
            cursor = body.get("pagination", {}).get("cursor")
            if not cursor:
                return
            if not isinstance(cursor, str) or cursor in seen:
                raise TwitchAPIError("Twitch repeated a pagination cursor; sync stopped.")
            seen.add(cursor)
            params["after"] = cursor


def _clips(api: _API, channel_id: str, start: datetime, end: datetime) -> list[dict]:
    """Split busy time windows before Helix's approximate 1,000-clip ceiling."""
    rows = []
    for row in api.pages(
        "clips", broadcaster_id=channel_id, started_at=_iso(start), ended_at=_iso(end)
    ):
        rows.append(row)
        if len(rows) >= 900:
            if end - start <= timedelta(seconds=1):
                raise TwitchAPIError("Twitch clip window is saturated; cannot sync it completely.")
            middle = start + (end - start) / 2
            return _clips(api, channel_id, start, middle) + _clips(api, channel_id, middle, end)
    return rows


def _known(api: _API, kind: str, ids: list[str]) -> list[dict]:
    rows = []
    for offset in range(0, len(ids), 100):
        rows.extend(
            api.page(
                kind,
                [("id", value) for value in ids[offset : offset + 100]],
                missing_videos_ok=kind == "videos",
            )["data"]
        )
    return rows


def _row(kind: str, item: dict, channel: dict, info: dict) -> dict:
    login = channel["login"]
    category = info.get("game_name", "") if kind == "channel" else item.get("game_id", "")
    details = {
        "kind": kind,
        "channel": login,
        "title": item.get("title", channel.get("display_name", login)),
        "description": item.get("description", ""),
        "category": category,
        "tags": info.get("tags", []) if kind == "channel" else [],
        "created_at": item.get("created_at", ""),
        "duration_seconds": _duration(item["duration"])
        if kind == "video"
        else item.get("duration", 0),
        "language": item.get("language", info.get("broadcaster_language", "")),
    }
    return {
        "id": f"{kind}:{item['id']}",
        "title": details["title"],
        "content": json.dumps(details, ensure_ascii=False, sort_keys=True),
        "url": item.get("url") or f"https://www.twitch.tv/{login}",
        "cognee_node_set": [f"channel:{login}"],
        "_deleted": False,
    }


def _sync(api: _API, state: dict, now: datetime, lookback_days: int) -> Iterator[dict]:
    users = api.get("users")  # Validate once per sync; also rejects app-only tokens.
    if len(users) != 1 or not users[0].get("id"):
        raise TwitchAuthError("Twitch requires a user access token, not an app access token.")
    user_id = users[0]["id"]
    if state.get("user_id", user_id) != user_id:
        raise TwitchAuthError(
            "This resource belongs to another Twitch user. Use a new resource_name."
        )
    follows = list(api.pages("channels/followed", user_id=user_id))
    previous = state.get("channels", {})
    current: dict = {}
    rows: dict = {}
    for follow in follows:
        channel_id = follow["broadcaster_id"]
        channel_users = api.get("users", {"id": channel_id})
        channel_infos = api.get("channels", {"broadcaster_id": channel_id})
        if not channel_users or not channel_infos:
            raise TwitchAPIError(
                "A followed channel is unavailable; retry before reconciling deletes."
            )
        channel, info = channel_users[0], channel_infos[0]
        old = previous.get(channel_id, {})
        start = (
            _timestamp(old["cursor"]) - timedelta(minutes=5)
            if old
            else (now - timedelta(days=lookback_days))
        )
        if start >= now:
            raise TwitchAPIError(
                "Twitch sync clock moved backwards; retry after the last sync time."
            )
        videos = {item["id"]: item for item in _known(api, "videos", old.get("videos", []))}
        # Get Videos has no started_at filter. Walk newest first to the watermark;
        # known ids above are checked separately so expiry is never inferred from a window.
        for video in api.pages("videos", user_id=channel_id, type="archive", sort="time"):
            if _timestamp(video["created_at"]) < start:
                break
            videos[video["id"]] = video
        clips = {item["id"]: item for item in _known(api, "clips", old.get("clips", []))}
        clips.update({item["id"]: item for item in _clips(api, channel_id, start, now)})
        channel_row = _row("channel", {**channel, "title": info.get("title", "")}, channel, info)
        rows[channel_row["id"]] = channel_row
        for kind, items in (("video", videos), ("clip", clips)):
            for item in items.values():
                row = _row(kind, item, channel, info)
                rows[row["id"]] = row
        current[channel_id] = {
            "cursor": _iso(now),
            "videos": sorted(videos),
            "clips": sorted(clips),
        }
    # All requests must succeed before emitting any row or changing the state.
    hashes = {
        key: hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
        for key, row in rows.items()
    }
    old_hashes = state.get("hashes", {})
    for key in sorted(set(old_hashes) - set(hashes)):
        yield {
            "id": key,
            "title": "",
            "content": "",
            "url": None,
            "cognee_node_set": [],
            "_deleted": True,
        }
    for key, row in rows.items():
        if hashes[key] != old_hashes.get(key):
            yield row
    state.update(user_id=user_id, channels=current, hashes=hashes)


def twitch_source(
    access_token: str,
    client_id: str,
    *,
    resource_name: str = "twitch_documents",
    lookback_days: int = 30,
    http_client: httpx.Client | None = None,
    check_active: Callable[[], None] | None = None,
) -> Any:
    """Build a DLT resource for ``await cognee.remember(source, write_disposition='merge')``.

    The token must belong to a user and include ``user:read:follows``. ``client_id``
    must identify the app which issued it. Initial VODs/clips cover ``lookback_days``;
    previously ingested content is retained until Twitch confirms deletion/unfollow.
    Keep the dataset and resource name stable across runs and unique per account.
    Injected HTTP clients remain caller-owned. Tokens are never saved in sync state.
    """
    import dlt
    from cognee.tasks.ingestion import dlt_utils
    from dlt.common.normalizers.naming.snake_case import NamingConvention

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 2:
        raise RuntimeError(
            "Twitch requires Cognee document sync v2 (per-row node sets). Upgrade Cognee."
        )
    if not access_token or not access_token.strip() or not client_id or not client_id.strip():
        raise ValueError("A Twitch user access token and matching Client ID are required.")
    if (
        not isinstance(resource_name, str)
        or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", resource_name)
        or NamingConvention().normalize_identifier(resource_name) != resource_name
    ):
        raise ValueError("resource_name must be a stable normalized lowercase DLT identifier.")
    if isinstance(lookback_days, bool) or not isinstance(lookback_days, int) or lookback_days <= 0:
        raise ValueError("lookback_days must be a positive integer.")

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns={
            "_deleted": {"data_type": "bool", "hard_delete": True},
            **dlt_utils.NODE_SET_COLUMN_HINT,
        },
    )
    def documents() -> Iterator[dict]:
        client = http_client if http_client is not None else httpx.Client()
        try:
            api = _API(client, client_id, access_token, check_active)
            yield from dlt_utils.guarded_rows(
                _sync(api, dlt.current.resource_state(), datetime.now(timezone.utc), lookback_days),
                check_active,
            )
        finally:
            if http_client is None:
                client.close()

    resource = documents()
    setattr(resource, dlt_utils.DOCUMENT_SOURCE_ATTR, "twitch")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    return resource
