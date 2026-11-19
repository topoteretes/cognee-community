"""An in-memory Mattermost server for the tests, with the behaviour seen on a real one.

Checked on mattermost-preview (Mattermost 11.11.1):

* ``GET /channels/{id}/posts?since=`` returns every post with ``update_at`` after
  ``since``, deleted ones included (``delete_at`` set, message blanked), at most 1000
  and without paging; the normal listing hides deleted posts.
* A new reply, an edit or a deleted reply also bumps the root's ``update_at``.
* An edit leaves a hidden copy of the old version: a new post with ``original_id``
  and ``delete_at`` set.
* Deleting a root deletes its replies in the same millisecond.
* System events are posts with a ``system_`` type.
* An archived channel is still readable, with ``delete_at`` set.
"""

import random

from cognee_community_connector_mattermost.mattermost import (
    MattermostAPIError,
    MattermostAuthError,
    MattermostRateLimitError,
)

SERVER = "https://chat.example.com"
TEAM = "t" + "0" * 24 + "1"
ALICE = {
    "id": "u" + "0" * 24 + "1",
    "username": "alice",
    "first_name": "Alice",
    "last_name": "Smith",
    "email": "alice@example.com",
}
BOB = {
    "id": "u" + "0" * 24 + "2",
    "username": "bob",
    "first_name": "",
    "last_name": "",
    "email": "bob@example.com",
}


def channel_id(n: int) -> str:
    return "c" + str(n).zfill(25)


def fail(status: int):
    if status == 401:
        raise MattermostAuthError("HTTP 401")
    if status == 429:
        raise MattermostRateLimitError("HTTP 429")
    raise MattermostAPIError(f"HTTP {status}", status, ends_run=status == 200)


class FakeMattermost:
    def __init__(self, now: int = 1_760_000_000_000):
        self.now = now
        self.posts: dict[str, dict] = {}
        self.channels: dict[str, dict] = {}
        self.users = {ALICE["id"]: ALICE, BOB["id"]: BOB}
        self.calls: list[tuple[str, str, dict]] = []
        self.fail: dict[str, int] = {}  # path -> HTTP status that request fails with
        self.since_order = "oldest"  # or "shuffled": a full since page in no order
        self.on_page = None  # called with the page number before each listing page
        self.date_header = True
        self.hide_from_listing: set[str] = set()  # posts a shifting page would skip
        self.stall_listing = False  # every listing page answers with the first one
        self._next = 0
        self.add_channel(1, "town-square", "Town Square")

    # -- building the server -------------------------------------------------
    def _id(self) -> str:
        self._next += 1
        return "p" + str(self._next).zfill(25)

    def tick(self) -> int:
        self.now += 1
        return self.now

    def add_channel(self, n: int, name: str, display: str, team: str | None = TEAM) -> str:
        cid = channel_id(n)
        self.channels[cid] = {
            "id": cid,
            "name": name,
            "display_name": display,
            "team_id": team or "",
            "delete_at": 0,
        }
        return cid

    def post(
        self,
        message: str,
        user=ALICE,
        root: str | None = None,
        channel: int = 1,
        type_: str = "",
        at: int | None = None,
    ) -> str:
        now = at if at is not None else self.tick()
        pid = self._id()
        self.posts[pid] = {
            "id": pid,
            "channel_id": channel_id(channel),
            "user_id": user["id"],
            "root_id": root or "",
            "message": message,
            "type": type_,
            "create_at": now,
            "update_at": now,
            "edit_at": 0,
            "delete_at": 0,
            "original_id": "",
            "file_ids": [],
        }
        if root:
            self.posts[root]["update_at"] = now
        return pid

    def edit(self, pid: str, message: str) -> None:
        now = self.tick()
        old = self.posts[pid]
        copy_id = self._id()
        self.posts[copy_id] = {
            **old,
            "id": copy_id,
            "original_id": pid,
            "delete_at": now,
            "update_at": now,
        }
        old.update(message=message, edit_at=now, update_at=now)
        if old["root_id"]:
            self.posts[old["root_id"]]["update_at"] = now

    def delete(self, pid: str) -> None:
        now = self.tick()
        post = self.posts[pid]
        targets = [pid]
        if not post["root_id"]:
            targets += [p["id"] for p in self.posts.values() if p["root_id"] == pid]
        else:
            self.posts[post["root_id"]]["update_at"] = now
        for target in targets:
            self.posts[target].update(delete_at=now, update_at=now, message="")

    def touch(self, pid: str) -> None:
        """A change that leaves the text as it was, like a reaction or a pin."""
        self.posts[pid]["update_at"] = self.tick()

    def archive(self, n: int = 1) -> None:
        self.channels[channel_id(n)]["delete_at"] = self.tick()

    # -- the API -------------------------------------------------------------
    def request(self, method, path, *, params=None, body=None):
        params = params or {}
        self.calls.append((method, path, params))
        if len(self.calls) > 300:
            raise AssertionError("FakeMattermost: more than 300 calls, the sync is looping")
        for prefix, status in self.fail.items():
            if path == prefix or path.startswith(prefix + "?"):
                fail(status)
        server_ms = self.now if self.date_header else None
        parts = path.strip("/").split("/")
        if method == "POST" and path == "/users/ids":
            return [self.users[u] for u in body if u in self.users], server_ms
        if parts[0] == "channels" and len(parts) == 2:
            channel = self.channels.get(parts[1])
            if channel is None:
                fail(404)
            return dict(channel), server_ms
        if parts[0] == "teams":
            return {"id": parts[1], "name": "eng", "display_name": "Engineering"}, server_ms
        if parts[0] == "channels" and parts[2:] == ["posts"]:
            return self._channel_posts(parts[1], params), server_ms
        if parts[0] == "posts" and parts[2:] == ["thread"]:
            root = self.posts.get(parts[1])
            if root is None or root["delete_at"]:
                fail(404)
            live = [
                p
                for p in self.posts.values()
                if (p["id"] == root["id"] or p["root_id"] == root["id"]) and not p["delete_at"]
            ]
            return {
                "order": [p["id"] for p in live],
                "posts": {p["id"]: dict(p) for p in live},
            }, server_ms
        raise AssertionError(f"FakeMattermost: unexpected {method} {path}")

    def _channel_posts(self, cid: str, params: dict) -> dict:
        mine = [p for p in self.posts.values() if p["channel_id"] == cid]
        if "since" in params:
            changed = sorted(
                (p for p in mine if p["update_at"] > params["since"]),
                key=lambda p: (p["create_at"], p["id"]),
            )
            if self.since_order == "shuffled":
                random.Random(7).shuffle(changed)
            changed = changed[:1000]
        else:
            if self.on_page is not None:
                self.on_page(params["page"])
            live = sorted(
                (p for p in mine if not p["delete_at"] and p["id"] not in self.hide_from_listing),
                key=lambda p: (p["create_at"], p["id"]),
                reverse=True,
            )
            start = 0 if self.stall_listing else params["page"] * params["per_page"]
            changed = live[start : start + params["per_page"]]
        return {"order": [p["id"] for p in changed], "posts": {p["id"]: dict(p) for p in changed}}
