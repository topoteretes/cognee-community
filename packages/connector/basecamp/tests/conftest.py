"""Shared fakes for the Basecamp connector tests (no network, no credentials).

``FakeBasecamp`` is an in-memory stand-in for the parts of the Basecamp API the
connector uses. It mirrors what was observed on a live account:

* ``/projects/recordings.json`` filters by ``type``, ``status`` and ``bucket``
  and sorts by ``updated_at`` (desc by default),
* pages are linked only through ``Link: <...>; rel="next"``,
* list responses carry a weak ``etag`` and honour ``If-None-Match`` with 304,
* trashing or archiving a recording bumps its ``updated_at``,
* emptying the trash removes the recording from every listing.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

ACCOUNT_ID = "999"
PROJECT = {"id": 1, "name": "Launch Project"}


class FakeBasecamp:
    def __init__(self, page_size: int = 2):
        self.page_size = page_size
        self.recordings: dict[int, dict] = {}
        self.clock = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
        self.requests: list[httpx.Request] = []
        self.next_id = 100

    # -- data helpers -------------------------------------------------------
    def _tick(self) -> str:
        self.clock += timedelta(minutes=10)
        return self.clock.isoformat().replace("+00:00", "Z")

    def add(self, rec_type: str, title: str, content: str = "", **extra) -> dict:
        self.next_id += 1
        ts = self._tick()
        rec = {
            "id": self.next_id,
            "type": rec_type,
            "status": "active",
            "title": title,
            "content": content,
            "created_at": ts,
            "updated_at": ts,
            "app_url": f"https://3.basecamp.com/{ACCOUNT_ID}/buckets/1/x/{self.next_id}",
            "bucket": dict(PROJECT),
            "creator": {"name": "Anand"},
            **extra,
        }
        self.recordings[rec["id"]] = rec
        return rec

    def edit(self, rec_id: int, **changes) -> None:
        self.recordings[rec_id].update(changes, updated_at=self._tick())

    def set_status(self, rec_id: int, status: str) -> None:
        self.edit(rec_id, status=status)
        # Comments follow their parent's status (inherits_status on the live API).
        for rec in self.recordings.values():
            if rec["type"] == "Comment" and (rec.get("parent") or {}).get("id") == rec_id:
                rec.update(status=status, updated_at=self._tick())

    def purge(self, rec_id: int) -> None:
        """Empty this item from the trash: gone from every listing."""
        self.recordings.pop(rec_id)

    # -- HTTP ---------------------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not request.headers.get("user-agent"):
            return httpx.Response(400, json={"error": "User-Agent required"})
        if request.url.path != f"/{ACCOUNT_ID}/projects/recordings.json":
            return httpx.Response(404, json={"status": 404, "error": "Not Found"})

        q = request.url.params
        status = q.get("status", "active")
        items = [
            r for r in self.recordings.values() if r["type"] == q["type"] and r["status"] == status
        ]
        if q.get("bucket"):
            buckets = set(q["bucket"].split(","))
            items = [r for r in items if str(r["bucket"]["id"]) in buckets]
        items.sort(
            key=lambda r: r[q.get("sort", "created_at")], reverse=q.get("direction") != "asc"
        )

        page = int(q.get("page", "1"))
        start = (page - 1) * self.page_size
        chunk = items[start : start + self.page_size]
        body = json.dumps(chunk).encode()
        etag = 'W/"' + hashlib.md5(json.dumps(items, sort_keys=True).encode()).hexdigest() + '"'
        if page == 1 and request.headers.get("if-none-match") == etag:
            return httpx.Response(304, headers={"etag": etag})

        headers = {"etag": etag, "x-total-count": str(len(items))}
        if start + self.page_size < len(items):
            next_url = request.url.copy_merge_params({"page": str(page + 1)})
            headers["link"] = f'<{next_url}>; rel="next"'
        return httpx.Response(200, content=body, headers=headers)

    def http_client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def fake() -> FakeBasecamp:
    return FakeBasecamp()


@pytest.fixture
def seeded(fake: FakeBasecamp) -> FakeBasecamp:
    """The same data that was created on the live test account."""
    msg = fake.add(
        "Message",
        "Q4 launch plan\n",
        '<p dir="auto">We will launch on Nov 15. '
        "The <strong>payments</strong> team owns checkout.</p>",
    )
    fake.add(
        "Comment",
        "",
        '<p dir="auto">Checkout needs a security review first.</p>',
        parent={"id": msg["id"], "type": "Message", "title": "Q4 launch plan\n"},
    )
    fake.add(
        "Document",
        "Onboarding guide",
        '<p dir="auto">Start with the <strong>staging</strong> setup.</p>',
    )
    todo_list = {"id": 50, "type": "Todolist", "title": "Launch tasks"}
    fake.add(
        "Todo",
        "Write release notes",
        "Write release notes",
        description='<p dir="auto">Include the Q4 changes.</p>',
        completed=True,
        due_on="2026-10-09",
        parent=todo_list,
    )
    bug = fake.add("Todo", "Fix login bug", "Fix login bug", completed=False, parent=todo_list)
    fake.add(
        "Comment",
        "",
        '<p dir="auto">Repro only on Safari.</p>',
        parent={"id": bug["id"], "type": "Todo", "title": "Fix login bug"},
    )
    return fake
