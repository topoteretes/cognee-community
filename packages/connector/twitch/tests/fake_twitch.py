"""An in-memory Twitch Helix API for tests (no token, no network).

The payloads mirror the shapes in the Helix reference: a user with three
followed channels — one with VODs (an archive, a highlight and an upload),
one with clips, one bare — pagination via the ``after`` cursor, a 401, and a
rate-limit wait that outlasts a run. Between runs the fake can unfollow a
channel, expire a VOD, add a VOD and rename a channel.

The classes at the bottom fake the HTTP layer (responses and errors with
rate-limit headers) for driving the real ``TwitchClient`` directly.
"""

import io
import json
import time
import urllib.error

from cognee_community_connector_twitch.twitch import (
    TwitchAuthError,
    TwitchRateLimitError,
)

USER = {
    "id": "111111",
    "login": "testuser",
    "display_name": "TestUser",
    "type": "",
    "broadcaster_type": "",
    "description": "Just here to watch.",
    "profile_image_url": "https://static-cdn.jtvnw.net/jtv_user_pictures/x-profile_image-300x300.png",
    "offline_image_url": "",
    "view_count": 0,
    "created_at": "2018-04-01T00:00:00Z",
}

OTHER_USER = {**USER, "id": "222222", "login": "somebodyelse", "display_name": "SomebodyElse"}


def follow(broadcaster_id, login, name, followed_at):
    return {
        "broadcaster_id": broadcaster_id,
        "broadcaster_login": login,
        "broadcaster_name": name,
        "followed_at": followed_at,
    }


FOLLOW_RUST = follow("2001", "ruststreams", "RustStreams", "2025-05-24T22:22:08Z")
FOLLOW_CODE = follow("2002", "codecorner", "CodeCorner", "2025-06-01T10:00:00Z")
FOLLOW_BARE = follow("2003", "barechannel", "BareChannel", "2024-01-01T00:00:00Z")


def channel_info(broadcaster_id, login, name, game_name, title):
    return {
        "broadcaster_id": broadcaster_id,
        "broadcaster_login": login,
        "broadcaster_name": name,
        "broadcaster_language": "en",
        "game_id": "509670" if game_name else "",
        "game_name": game_name,
        "title": title,
        "delay": 0,
        "tags": [],
        "content_classification_labels": [],
        "is_branded_content": False,
    }


INFO_RUST = channel_info(
    "2001", "ruststreams", "RustStreams", "Science & Technology", "Building a compiler in Rust"
)
INFO_CODE = channel_info(
    "2002", "codecorner", "CodeCorner", "Software Development", "Live coding: parsers"
)
INFO_BARE = channel_info("2003", "barechannel", "BareChannel", "", "")

# GET /users owns the channel description; GET /channels does not return one.
USERS = {
    "2001": {
        "id": "2001",
        "login": "ruststreams",
        "display_name": "RustStreams",
        "description": "Streams about the Rust programming language.",
    },
    "2002": {
        "id": "2002",
        "login": "codecorner",
        "display_name": "CodeCorner",
        "description": "Live coding most evenings.",
    },
    "2003": {
        "id": "2003",
        "login": "barechannel",
        "display_name": "BareChannel",
        "description": "",
    },
}


def video(video_id, title, description, type_, duration, published_at, view_count):
    return {
        "id": video_id,
        "stream_id": "998877" if type_ == "archive" else None,
        "user_id": "2001",
        "user_login": "ruststreams",
        "user_name": "RustStreams",
        "title": title,
        "description": description,
        "created_at": published_at,
        "published_at": published_at,
        "url": f"https://www.twitch.tv/videos/{video_id}",
        "thumbnail_url": f"https://static-cdn.jtvnw.net/cf_vods/thumb/{video_id}-%{{width}}x%{{height}}.jpg",
        "viewable": "public",
        "view_count": view_count,
        "language": "en",
        "type": type_,
        "duration": duration,
        "muted_segments": [],
    }


VIDEO_RUST_ARCHIVE = video(
    "1234567",
    "Building a compiler, part 3",
    "Type checking and monomorphization.",
    "archive",
    "PT2H14M33S",
    "2025-09-01T20:05:30Z",
    1863062,
)
VIDEO_RUST_HIGHLIGHT = video(
    "1234568",
    "The borrow checker finally approves",
    "Best moments of part 3.",
    "highlight",
    "1h2m3s",
    "2025-09-02T12:00:00Z",
    4200,
)
VIDEO_RUST_UPLOAD = video(
    "1234569",
    "Setup tour",
    "My keyboard and editor setup.",
    "upload",
    "3m21s",
    "2025-08-20T09:30:00Z",
    777,
)
VIDEO_RUST_NEW = video(
    "1234570",
    "Building a compiler, part 4",
    "Code generation begins.",
    "archive",
    "PT1H30M",
    "2025-09-08T21:00:00Z",
    5,
)


def clip(clip_id, title, creator_name, view_count, created_at):
    return {
        "id": clip_id,
        "url": f"https://clips.twitch.tv/{clip_id}",
        "embed_url": f"https://clips.twitch.tv/embed?clip={clip_id}",
        "broadcaster_id": "2002",
        "broadcaster_name": "CodeCorner",
        "creator_id": "3001",
        "creator_name": creator_name,
        "video_id": "205586603",
        "game_id": "33103",
        "language": "en",
        "title": title,
        "view_count": view_count,
        "created_at": created_at,
        "thumbnail_url": f"https://clips-media-assets.twitch.tv/{clip_id}-preview-480x272.jpg",
        "duration": 12.9,
        "vod_offset": 1957,
        "is_featured": False,
    }


CLIP_PARSER = clip(
    "RandomClip1", "The parser finally works", "ClipFan", 10, "2025-08-20T22:34:18Z"
)
CLIP_TYPES = clip(
    "RandomClip2", "Explaining variance in 30 seconds", "TypeNerd", 9, "2025-08-25T19:00:00Z"
)


class FakeTwitch:
    """In-memory Helix: follows, channel info, user descriptions, videos, clips.

    Lists are served in fixed pages through ``after`` cursors like the real
    API, so pagination is observable. ``auth_error`` makes the token check
    (GET /users without ids) fail like a 401; ``long_wait`` raises
    :class:`TwitchRateLimitError` for one ``(path, param value)`` pair, which
    is what the real client does when a wait outlasts a run.
    """

    def __init__(self, user=USER, follows=None, page_size=2):
        self.user = dict(user)
        # Payloads are copied so a test's between-run changes never leak into
        # another test's fake.
        if follows is None:
            follows = [FOLLOW_CODE, FOLLOW_RUST, FOLLOW_BARE]  # descending by followed_at
        self.follows = [dict(item) for item in follows]
        self.channel_info = {
            info["broadcaster_id"]: dict(info) for info in (INFO_RUST, INFO_CODE, INFO_BARE)
        }
        self.users = {key: dict(value) for key, value in USERS.items()}
        self.videos = {
            "2001": [
                dict(item)
                for item in (VIDEO_RUST_ARCHIVE, VIDEO_RUST_HIGHLIGHT, VIDEO_RUST_UPLOAD)
            ]
        }
        self.clips = {"2002": [dict(CLIP_PARSER), dict(CLIP_TYPES)]}
        self.page_size = page_size
        self.auth_error = False
        self.auth_fail_on = None  # (path, value) -> raise TwitchAuthError, like a revoked token
        self.long_wait = None  # (path, value) -> raise TwitchRateLimitError, like a long wait
        self.calls = []

    # -- between-run changes ------------------------------------------------
    def unfollow(self, broadcaster_id):
        self.follows = [f for f in self.follows if f["broadcaster_id"] != broadcaster_id]

    def expire_video(self, video_id):
        self.videos = {b: [v for v in vs if v["id"] != video_id] for b, vs in self.videos.items()}

    def add_video(self, broadcaster_id, payload):
        self.videos.setdefault(broadcaster_id, []).insert(0, dict(payload))

    def rename_channel(self, broadcaster_id, new_login, new_name):
        for f in self.follows:
            if f["broadcaster_id"] == broadcaster_id:
                f["broadcaster_login"] = new_login
                f["broadcaster_name"] = new_name
        info = self.channel_info[broadcaster_id]
        info["broadcaster_login"], info["broadcaster_name"] = new_login, new_name
        self.users.setdefault(broadcaster_id, {})["login"] = new_login

    # -- the Helix surface ---------------------------------------------------
    def get(self, path, **params):
        self.calls.append((path, params))
        blocked = self.auth_fail_on
        if blocked is not None and path == blocked[0] and blocked[1] in params.values():
            raise TwitchAuthError("Twitch rejected the access token.")
        wait = self.long_wait
        if wait is not None and path == wait[0] and wait[1] in params.values():
            raise TwitchRateLimitError(f"Twitch {path}: rate-limit wait of 3600s.", 3600)
        if path == "users":
            ids = params.get("id")
            if not ids:
                if self.auth_error:
                    raise TwitchAuthError("Twitch rejected the access token.")
                return {"data": [self.user]}
            return {"data": [self.users[i] for i in ids if i in self.users]}
        if path == "channels/followed":
            return self._page(self.follows, params)
        if path == "channels":
            return {"data": [self.channel_info[i] for i in params.get("broadcaster_id", [])]}
        if path == "videos":
            return self._page(self.videos.get(params.get("user_id"), []), params)
        if path == "clips":
            return self._page(self.clips.get(params.get("broadcaster_id"), []), params)
        raise AssertionError(path)

    def _page(self, items, params):
        start = int(params.get("after") or 0)
        page = items[start : start + self.page_size]
        payload = {"data": list(page)}
        if start + self.page_size < len(items):
            payload["pagination"] = {"cursor": str(start + self.page_size)}
        return payload

    def calls_to(self, path):
        return [params for called, params in self.calls if called == path]


# -- HTTP-layer fakes, for the real TwitchClient ----------------------------
class Headers(dict):
    """Case-insensitive header lookup, like a real response's."""

    def get(self, name, default=None):
        for key, value in self.items():
            if key.lower() == str(name).lower():
                return value
        return default


class Response(io.BytesIO):
    """A 200 JSON response body with optional rate-limit headers."""

    def __init__(self, payload, remaining=None, reset=None):
        super().__init__(json.dumps(payload).encode())
        headers = Headers()
        if remaining is not None:
            headers["Ratelimit-Remaining"] = str(remaining)
        if reset is not None:
            headers["Ratelimit-Reset"] = str(int(reset))
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def http_error(status, payload=None, remaining=None, reset=None, url="https://api.twitch.tv/helix/videos"):
    """An HTTPError the way urlopen raises it, with rate-limit headers."""
    headers = Headers()
    if remaining is not None:
        headers["Ratelimit-Remaining"] = str(remaining)
    if reset is not None:
        headers["Ratelimit-Reset"] = str(int(reset))
    body = io.BytesIO(json.dumps(payload or {}).encode())
    error = urllib.error.HTTPError(url, status, "err", headers, body)
    error.headers = headers
    return error


class Opener:
    """Serves queued outcomes (responses or exceptions) as urllib's opener would."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)

    def __call__(self, request):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def in_seconds(seconds):
    return time.time() + seconds
