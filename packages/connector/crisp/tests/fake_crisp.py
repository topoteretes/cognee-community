"""An in-memory Crisp REST API for the tests, with the behaviour seen on a real workspace.

Checked on a free Crisp workspace with a website token (see the issue, cognee#4737):

* A conversation's ``updated_at`` only moves when a new message arrives. Editing a
  message (old or newest), editing a note, removing a message, changing the visitor's
  nickname or segments, and resolving do not move it. An operator reply on a resolved
  conversation moves it and makes it ``unresolved`` again.
* The list shows the current ``state`` and visitor ``meta``; resolving adds an
  ``event`` message (``{"namespace": "state:resolved"}``).
* Empty conversations are hidden unless ``include_empty=1``.
* Messages come back oldest first, the newest batch by default; ``timestamp_before``
  pages back.
* HEAD of a removed conversation, or of a made-up session id, answers 404.
"""

import uuid

from cognee_community_connector_crisp.crisp import (
    CrispAPIError,
    CrispAuthError,
    CrispRateLimitError,
)

WEBSITE = "8c842203-7ed8-4e29-a608-7cf78a7d2fcc"


def fail(status: int):
    if status in (401, 403):
        raise CrispAuthError(f"HTTP {status}", status)
    if status in (420, 429):
        raise CrispRateLimitError(f"HTTP {status}")
    raise CrispAPIError(f"HTTP {status}", status)


class FakeCrisp:
    def __init__(self, list_page: int = 20, message_page: int = 40):
        self.now = 1_760_000_000_000
        self.list_page = list_page
        self.message_page = message_page
        self.conversations: dict[str, dict] = {}
        self.messages: dict[str, list[dict]] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail: dict[str, int] = {}
        self._fp = 1000

    # -- building the workspace ------------------------------------------------
    def tick(self) -> int:
        self.now += 1000
        return self.now

    def conversation(
        self, text: str | None = "Hi, my payment failed.", nickname: str = "Jane Doe"
    ) -> str:
        sid = f"session_{uuid.uuid4()}"
        created = self.tick()
        self.conversations[sid] = {
            "session_id": sid,
            "website_id": WEBSITE,
            "state": "pending",
            "created_at": created,
            "updated_at": created,
            "meta": {
                "nickname": nickname,
                "email": "jane@example.com",
                "phone": "+15550100",
                "ip": "203.0.113.5",
                "segments": [],
                "device": {"geolocation": {"country": "DE", "city": "Berlin"}},
            },
        }
        self.messages[sid] = []
        if text is not None:
            self.visitor(sid, text)
        return sid

    def _message(self, sid: str, **fields) -> int:
        self._fp += 1
        message = {"fingerprint": self._fp, "session_id": sid, "timestamp": self.tick(), **fields}
        self.messages[sid].append(message)
        return self._fp

    def visitor(self, sid: str, text: str) -> int:
        fp = self._message(
            sid,
            type="text",
            origin="chat",
            **{"from": "user"},
            content=text,
            user={"nickname": self.conversations[sid]["meta"]["nickname"], "user_id": "visitor"},
        )
        self.conversations[sid]["updated_at"] = self.now
        return fp

    def operator(self, sid: str, text: str, name: str = "Alice Smith") -> int:
        fp = self._message(
            sid,
            type="text",
            origin="chat",
            **{"from": "operator"},
            content=text,
            user={"nickname": name, "user_id": "op1"},
        )
        self.conversations[sid]["updated_at"] = self.now
        if self.conversations[sid]["state"] == "resolved":
            self.conversations[sid]["state"] = "unresolved"
        return fp

    def note(self, sid: str, text: str, name: str = "Alice Smith") -> int:
        fp = self._message(
            sid,
            type="note",
            origin="chat",
            **{"from": "operator"},
            content=text,
            user={"nickname": name, "user_id": "op1"},
        )
        self.conversations[sid]["updated_at"] = self.now
        return fp

    def file(self, sid: str, name: str) -> int:
        fp = self._message(
            sid,
            type="file",
            origin="chat",
            **{"from": "user"},
            content={"name": name, "url": "https://x/y", "type": "application/pdf"},
            user={},
        )
        self.conversations[sid]["updated_at"] = self.now
        return fp

    def edit(self, sid: str, fp: int, text: str) -> None:
        # Editing does not move updated_at.
        message = next(m for m in self.messages[sid] if m["fingerprint"] == fp)
        message["content"], message["edited"] = text, True

    def remove(self, sid: str, fp: int) -> None:
        # Removing does not move updated_at either.
        self.messages[sid] = [m for m in self.messages[sid] if m["fingerprint"] != fp]

    def resolve(self, sid: str) -> None:
        self.conversations[sid]["state"] = "resolved"
        self._message(
            sid,
            type="event",
            **{"from": "operator"},
            content={"namespace": "state:resolved"},
            user={},
        )

    def rename(self, sid: str, nickname: str) -> None:
        self.conversations[sid]["meta"]["nickname"] = nickname

    def delete(self, sid: str) -> None:
        del self.conversations[sid]
        del self.messages[sid]

    # -- the API ---------------------------------------------------------------
    def request(self, method: str, path: str, params: dict | None = None):
        params = params or {}
        self.calls.append((method, path))
        if len(self.calls) > 3000:
            raise AssertionError("runaway: more than 3000 calls")
        if path in self.fail:
            fail(self.fail[path])
        parts = path.strip("/").split("/")
        assert parts[:2] == ["website", WEBSITE], path
        if parts[2] == "conversations":
            page = int(parts[3])
            items = sorted(self.conversations.values(), key=lambda c: -c["updated_at"])
            if not int(params.get("include_empty") or 0):
                items = [c for c in items if self.messages[c["session_id"]]]
            size = min(int(params.get("per_page") or 20), self.list_page)
            chunk = items[(page - 1) * size : page * size]
            return [
                {**c, "meta": {**c["meta"], "segments": list(c["meta"]["segments"])}} for c in chunk
            ]
        if parts[2] == "conversation":
            sid = parts[3]
            if method == "HEAD":
                return 200 if sid in self.conversations else 404
            if sid not in self.conversations:
                fail(404)
            if parts[4:] == ["messages"]:
                before = params.get("timestamp_before")
                items = sorted(self.messages[sid], key=lambda m: m["timestamp"])
                if before is not None:
                    items = [m for m in items if m["timestamp"] < int(before)]
                return [dict(m) for m in items[-self.message_page :]]
        raise AssertionError(f"unexpected {method} {path}")
