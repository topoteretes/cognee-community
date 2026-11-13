"""An in-memory Front Core API for the tests, with the behaviour seen on a real account.

Checked on a Front developer account (see the connector issue, cognee#4734):

* A conversation's ``updated_at`` moves when a comment is added, edited or deleted,
  a message is deleted, the subject is edited, it is trashed, restored, moved, or
  another conversation is merged into it. Adding or renaming a tag does not move it;
  the listing returns the current tag names inline.
* The inbox listing is not ordered by ``updated_at``. A trashed conversation stays
  in it with ``status="deleted"``; a permanently deleted, merged-away or moved-out
  one drops out.
* GET of a permanently deleted conversation answers 404, of a merged-away one 301
  to the survivor, of a moved one 200 (and its inboxes list the new inbox).
* Comment bodies typed in the app carry HTML entities (``&#x20;``).
* Listings page with ``_pagination.next``, a full URL with a ``page_token``.
"""

import urllib.parse

from cognee_community_connector_front.front import (
    FrontAPIError,
    FrontAuthError,
    FrontRateLimitError,
)

API = "https://api2.frontapp.com"
ME = {"id": "cmp_acme", "name": "Acme"}
ALICE = {"id": "tea_1", "first_name": "Alice", "last_name": "Smith", "email": "alice@acme.test"}
BOB = {"id": "tea_2", "first_name": "", "last_name": "", "username": "bob", "email": "b@acme.test"}


def fail(status: int, location: str | None = None):
    if status == 401:
        raise FrontAuthError("HTTP 401")
    if status == 429:
        raise FrontRateLimitError("HTTP 429")
    raise FrontAPIError(f"HTTP {status}", status, ends_run=status == 200, location=location)


class FakeFront:
    api_url = API

    def __init__(self, page_size: int = 100):
        self.now = 1_760_000_000.0
        self.page_size = page_size
        self.inboxes: dict[str, dict] = {}
        self.conversations: dict[str, dict] = {}
        self.membership: dict[str, set[str]] = {}  # conversation -> inboxes
        self.messages: dict[str, list[dict]] = {}
        self.comments: dict[str, list[dict]] = {}
        self.merged_into: dict[str, str] = {}
        self.calls: list[str] = []
        self.fail: dict[str, int] = {}  # path -> HTTP status that request fails with
        self.me = dict(ME)
        self._next = 0
        self.add_inbox("inb_support", "Support")

    # -- building the account ------------------------------------------------
    def _id(self, prefix: str) -> str:
        self._next += 1
        return f"{prefix}_{self._next}"

    def tick(self) -> float:
        self.now += 1.5
        return self.now

    def add_inbox(self, inbox_id: str, name: str) -> str:
        self.inboxes[inbox_id] = {"id": inbox_id, "name": name}
        return inbox_id

    def conversation(
        self,
        subject: str,
        customer: str = "Jane Doe",
        text: str = "Hello, my order never arrived.",
        inbox: str = "inb_support",
        tags: tuple[str, ...] = (),
    ) -> str:
        cid = self._id("cnv")
        self.conversations[cid] = {
            "id": cid,
            "subject": subject,
            "status": "unassigned",
            "tags": [{"id": f"tag_{t}", "name": t} for t in tags],
            "assignee": None,
            "created_at": self.tick(),
            "updated_at": self.now,
        }
        self.membership[cid] = {inbox}
        self.messages[cid] = []
        self.comments[cid] = []
        self.inbound(cid, text, customer)
        return cid

    def _touch(self, cid: str) -> None:
        self.conversations[cid]["updated_at"] = self.tick()

    def inbound(self, cid: str, text: str, customer: str = "Jane Doe") -> str:
        mid = self._id("msg")
        self.messages[cid].append(
            {
                "id": mid,
                "is_inbound": True,
                "is_draft": False,
                "created_at": self.tick(),
                "author": None,
                "recipients": [
                    {"handle": "jane@example.com", "role": "from", "name": customer},
                    {"handle": "support@acme.test", "role": "to", "name": None},
                ],
                "text": text,
                "body": f"<p>{text}</p>",
                "attachments": [],
            }
        )
        self._touch(cid)
        return mid

    def reply(self, cid: str, text: str, author: dict = ALICE, draft: bool = False) -> str:
        mid = self._id("msg")
        self.messages[cid].append(
            {
                "id": mid,
                "is_inbound": False,
                "is_draft": draft,
                "created_at": self.tick(),
                "author": author,
                "recipients": [{"handle": "jane@example.com", "role": "to", "name": "Jane"}],
                "text": text,
                "body": f"<div>{text}</div>",
                "attachments": [],
            }
        )
        self._touch(cid)
        return mid

    def comment(self, cid: str, body: str, author: dict = ALICE) -> str:
        com = self._id("com")
        self.comments[cid].append(
            {"id": com, "author": author, "body": body, "posted_at": self.tick()}
        )
        self._touch(cid)
        return com

    def edit_comment(self, cid: str, com: str, body: str) -> None:
        next(c for c in self.comments[cid] if c["id"] == com)["body"] = body
        self._touch(cid)

    def delete_comment(self, cid: str, com: str) -> None:
        self.comments[cid] = [c for c in self.comments[cid] if c["id"] != com]
        self._touch(cid)

    def delete_message(self, cid: str, mid: str) -> None:
        self.messages[cid] = [m for m in self.messages[cid] if m["id"] != mid]
        self._touch(cid)

    def tag(self, cid: str, name: str) -> None:
        # A tag change does not move updated_at.
        self.conversations[cid]["tags"].append({"id": f"tag_{name}", "name": name})

    def rename_tag(self, old: str, new: str) -> None:
        for c in self.conversations.values():
            for t in c["tags"]:
                if t["name"] == old:
                    t["name"] = new

    def set_status(self, cid: str, status: str) -> None:
        self.conversations[cid]["status"] = status
        self._touch(cid)

    def purge(self, cid: str) -> None:
        """Permanent delete: GET answers 404 and the listing drops it."""
        del self.conversations[cid]
        del self.membership[cid]

    def merge(self, gone: str, survivor: str) -> None:
        self.messages[survivor] += self.messages.pop(gone)
        self.comments[survivor] += self.comments.pop(gone)
        del self.conversations[gone]
        del self.membership[gone]
        self.merged_into[gone] = survivor
        self._touch(survivor)

    def move(self, cid: str, inbox: str) -> None:
        self.membership[cid] = {inbox}
        self._touch(cid)

    # -- the API ---------------------------------------------------------------
    def _page(self, path: str, items: list[dict], params: dict) -> dict:
        start = int(params.get("page_token") or 0)
        limit = min(int(params.get("limit") or 50), self.page_size)
        page = items[start : start + limit]
        nxt = None
        if start + limit < len(items):
            query = urllib.parse.urlencode({"limit": limit, "page_token": start + limit})
            nxt = f"{API}{path}?{query}"
        return {"_results": [dict(i) for i in page], "_pagination": {"next": nxt}}

    def get(self, path: str, params: dict | None = None) -> dict:
        params = params or {}
        self.calls.append(path)
        if len(self.calls) > 2000:
            raise AssertionError("runaway: more than 2000 calls")
        if path in self.fail:
            fail(self.fail[path])
        parts = path.strip("/").split("/")
        if parts == ["me"]:
            return dict(self.me)
        if parts[0] == "inboxes" and len(parts) == 2:
            if parts[1] not in self.inboxes:
                fail(404)
            return dict(self.inboxes[parts[1]])
        if parts[0] == "inboxes" and parts[2:] == ["conversations"]:
            # Not ordered by updated_at: by id, newest last, like a fixed index.
            items = [
                self.conversations[c]
                for c in sorted(self.conversations, reverse=True)
                if parts[1] in self.membership[c]
            ]
            return self._page(path, items, params)
        if parts[0] == "conversations":
            cid = parts[1]
            if cid in self.merged_into:
                fail(301, location=f"{API}/conversations/{self.merged_into[cid]}")
            if cid not in self.conversations:
                fail(404)
            if len(parts) == 2:
                return dict(self.conversations[cid])
            if parts[2] == "messages":
                # Newest first, like Front.
                return self._page(path, list(reversed(self.messages[cid])), params)
            if parts[2] == "comments":
                return self._page(path, list(reversed(self.comments[cid])), params)
            if parts[2] == "inboxes":
                items = [self.inboxes[i] for i in sorted(self.membership[cid])]
                return self._page(path, items, params)
        raise AssertionError(f"unexpected path {path}")
