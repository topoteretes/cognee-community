"""Shared fakes for the Twist connector tests (no network, no credentials).

``FakeTwist`` is an in-memory stand-in for the parts of the Twist v3 API the
connector uses. Every behaviour below was observed on a live free workspace:

* ``channels/get`` returns archived channels too, with ``archived: true``;
* ``threads/get`` sorts by ``last_updated_ts`` (newest first) until ``before_id``
  or ``after_id`` is given, and by id (highest first) after that; ``limit``
  above 500 is rejected with error 20;
* ``comments/get`` returns comments by ``obj_index``; ``from_obj_index`` and
  ``to_obj_index`` are an inclusive range, and ``limit`` keeps the last N;
* a new comment bumps the thread's ``version``, ``last_updated_ts``,
  ``comment_count`` and ``last_obj_index``;
* editing an earlier comment changes only that comment, while editing the
  latest one also bumps the thread's ``version``;
* editing the opening post bumps ``version`` and ``last_edited_ts`` only;
* renaming, moving and closing a thread add a system-message comment
  (``version`` +2, ``comment_count`` +1); only the rename moves
  ``last_updated_ts``, and a moved thread keeps its id;
* a deleted comment stays listed with ``deleted: true`` and content ``"-"``;
  the thread's ``version`` goes up by one and nothing else changes;
* a deleted thread is gone from listings, and reading it gives 404
  (error 110 for the thread, 108 for its comments);
* a rejected token gives HTTP 403 with error code 200.
"""

from __future__ import annotations

import httpx
import pytest

WORKSPACE_ID = 254427
TOKEN = "oauth2:test-token"


def _error(status: int, code: int, message: str) -> httpx.Response:
    return httpx.Response(
        status,
        json={"error_code": code, "error_extra": {}, "error_string": message},
    )


class FakeTwist:
    token = TOKEN
    workspace_id = WORKSPACE_ID

    def __init__(self):
        self.clock = 1_791_660_000
        self.next_thread_id = 8_108_000
        self.next_comment_id = 103_777_000
        self.channels: dict[int, dict] = {}
        self.threads: dict[int, dict] = {}
        self.comments: dict[int, list[dict]] = {}
        self.requests: list[httpx.Request] = []
        self.fail_with: int | None = None
        self.hidden_comment_threads: set[int] = set()
        self.forbidden_channels: set[int] = set()

    # -- data helpers -------------------------------------------------------
    def _tick(self) -> int:
        self.clock += 60
        return self.clock

    def add_channel(self, channel_id: int, name: str, **extra) -> dict:
        channel = {
            "id": channel_id,
            "name": name,
            "description": "",
            "public": True,
            "archived": False,
            "workspace_id": WORKSPACE_ID,
            "version": 0,
            **extra,
        }
        self.channels[channel_id] = channel
        return channel

    def add_thread(self, channel_id: int, title: str, content: str = "", **extra) -> dict:
        self.next_thread_id += 1
        ts = self._tick()
        thread = {
            "id": self.next_thread_id,
            "channel_id": channel_id,
            "workspace_id": WORKSPACE_ID,
            "title": title,
            "content": content,
            "creator": 962248,
            "creator_name": "Anand",
            "posted_ts": ts,
            "last_updated_ts": ts,
            "last_edited_ts": None,
            "comment_count": 0,
            "last_obj_index": -1,
            "closed": False,
            "is_archived": True,  # the creator's Inbox "done" state, not an archive
            "attachments": [],
            "version": 0,
            **extra,
        }
        self.threads[thread["id"]] = thread
        self.comments[thread["id"]] = []
        return thread

    def _append_comment(
        self, thread_id: int, content: str, bump_updated: bool = True, **extra
    ) -> dict:
        thread = self.threads[thread_id]
        self.next_comment_id += 1
        ts = self._tick()
        comment = {
            "id": self.next_comment_id,
            "thread_id": thread_id,
            "channel_id": thread["channel_id"],
            "obj_index": len(self.comments[thread_id]),
            "content": content,
            "creator": 962248,
            "creator_name": "Anand",
            "posted_ts": ts,
            "last_edited_ts": None,
            "deleted": False,
            "deleted_by": None,
            "system_message": None,
            "attachments": [],
            "version": 0,
            **extra,
        }
        self.comments[thread_id].append(comment)
        thread.update(
            comment_count=thread["comment_count"] + 1, last_obj_index=comment["obj_index"]
        )
        if bump_updated:
            thread["last_updated_ts"] = ts
        return comment

    def add_comment(self, thread_id: int, content: str, **extra) -> dict:
        comment = self._append_comment(thread_id, content, **extra)
        self.threads[thread_id]["version"] += 1
        return comment

    def _system_comment(
        self, thread_id: int, kind: str, text: str, bump_updated: bool = False
    ) -> None:
        # Live: a rename moved last_updated_ts, a move or a close did not.
        self._append_comment(
            thread_id, text, bump_updated=bump_updated, system_message={"type": kind}
        )
        self.threads[thread_id]["version"] += 2

    def edit_comment(self, thread_id: int, comment_id: int, content: str) -> None:
        comments = self.comments[thread_id]
        for comment in comments:
            if comment["id"] == comment_id:
                comment.update(
                    content=content, last_edited_ts=self._tick(), version=comment["version"] + 1
                )
        # The thread carries its latest comment, so only that edit moves it.
        if comments and comments[-1]["id"] == comment_id:
            self.threads[thread_id]["version"] += 1

    def edit_thread_body(self, thread_id: int, content: str) -> None:
        thread = self.threads[thread_id]
        thread.update(content=content, last_edited_ts=self._tick(), version=thread["version"] + 1)

    def rename(self, thread_id: int, title: str) -> None:
        old = self.threads[thread_id]["title"]
        self.threads[thread_id]["title"] = title
        self._system_comment(
            thread_id,
            "THREAD_TITLE_UPDATED",
            f"Anand changed the subject from {old} to {title}",
            bump_updated=True,
        )

    def delete_comment(self, thread_id: int, comment_id: int) -> None:
        for comment in self.comments[thread_id]:
            if comment["id"] == comment_id:
                comment.update(deleted=True, deleted_by=962248, content="-")
        self.threads[thread_id]["version"] += 1

    def move(self, thread_id: int, channel_id: int) -> None:
        self.threads[thread_id]["channel_id"] = channel_id
        name = self.channels[channel_id]["name"]
        self._system_comment(thread_id, "THREAD_MOVED", f"Anand moved this thread to #{name}")

    def close(self, thread_id: int) -> None:
        self.threads[thread_id]["closed"] = True
        self._system_comment(thread_id, "THREAD_CLOSED", "")

    def delete_thread(self, thread_id: int) -> None:
        self.threads.pop(thread_id)
        self.comments.pop(thread_id)

    # -- HTTP ---------------------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return _error(403, 200, "Invalid token")
        if self.fail_with is not None:
            return httpx.Response(self.fail_with, text="unavailable")

        q = request.url.params
        path = request.url.path.removeprefix("/api/")
        if "limit" in q and int(q["limit"]) > 500:
            return _error(400, 20, "Invalid argument value")

        if path == "v3/workspaces/getone":
            if int(q["id"]) != WORKSPACE_ID:
                return _error(404, 110, "Resource not found")
            return httpx.Response(200, json={"id": WORKSPACE_ID, "name": "Anand Test"})

        if path == "v3/channels/get":
            return httpx.Response(200, json=list(self.channels.values()))

        if path == "v3/threads/get":
            channel_id = int(q["channel_id"])
            if channel_id in self.forbidden_channels:
                return _error(403, 109, "Forbidden")
            if channel_id not in self.channels:
                return _error(404, 110, f"Channel {channel_id} not found")
            items = [t for t in self.threads.values() if t["channel_id"] == channel_id]
            if "before_id" in q or "after_id" in q:
                items.sort(key=lambda t: t["id"], reverse=True)
                if "before_id" in q:
                    items = [t for t in items if t["id"] < int(q["before_id"])]
                if "after_id" in q:
                    items = [t for t in items if t["id"] > int(q["after_id"])]
            else:
                items.sort(key=lambda t: t["last_updated_ts"], reverse=True)
            return httpx.Response(200, json=items[: int(q.get("limit", 20))])

        if path == "v3/comments/get":
            thread_id = int(q["thread_id"])
            if thread_id not in self.threads or thread_id in self.hidden_comment_threads:
                return _error(404, 108, "Thread not found")
            items = self.comments[thread_id]
            low = int(q.get("from_obj_index", 0))
            high = int(q.get("to_obj_index", len(items)))
            items = [c for c in items if low <= c["obj_index"] <= high]
            return httpx.Response(200, json=items[-int(q.get("limit", 20)) :])

        return _error(404, 110, "Resource not found")

    def http_client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def fake() -> FakeTwist:
    return FakeTwist()


@pytest.fixture
def seeded(fake: FakeTwist) -> FakeTwist:
    """The same data that was created on the live test workspace."""
    fake.add_channel(852087, "launch", description="Launch planning")
    fake.add_channel(852086, "design")
    checkout = fake.add_thread(
        852087,
        "Checkout redesign",
        "_We need to pick between option A (single page) and option B (two steps)._",
    )
    fake.add_comment(checkout["id"], "Option B tested better with users.")
    fake.add_comment(checkout["id"], "Agreed, let's go with option B.")
    fake.add_thread(852087, "Launch date", "Target launch is 1 March.")
    fake.add_thread(852086, "Logo refresh", "New logo drafts are due Friday.")
    return fake


@pytest.fixture
def thread_named(seeded: FakeTwist):
    """Look up a seeded thread by its title."""

    def lookup(title: str) -> dict:
        return next(t for t in seeded.threads.values() if t["title"] == title)

    return lookup
