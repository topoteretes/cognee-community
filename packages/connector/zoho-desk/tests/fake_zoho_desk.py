"""An in-memory Zoho Desk API for the tests, with the behaviour seen on a real account.

Checked on a Zoho Desk account (Enterprise trial, ``.in`` data center; see the issue,
cognee#6034):

* Every call needs the ``orgId`` header; ``/organizations`` gives it.
* ``/tickets/search?modifiedTimeRange=<from>,<to>`` returns tickets modified in the
  window, with ``modifiedTime``. The plain listing only returns ``modifiedTime`` when
  asked for it with ``fields=``.
* A new comment (or reply) moves the ticket's ``modifiedTime``, and
  ``/tickets/{id}/conversations`` returns threads and comments together. A thread's full
  text is read from ``/tickets/{id}/threads/{thread}``; the conversation lists a summary.
* ``/articles?sortBy=-modifiedTime`` returns articles newest first, with ``status``.
* A deleted ticket shows in ``/recycleBin`` with ``type: "Tickets"``, its id and
  ``deletedTime``; right afterwards the Search API can still return it, and its
  conversation answers 403.
* A deleted article does not: it goes to the Knowledge Base's own trash, drops out of
  ``/articles``, and ``GET /articles/{id}`` still answers 200 with ``isTrashed: true``.
* An empty listing answers 204 (the client turns it into an empty payload), and every
  answer reports the day's remaining credits.
"""

from datetime import datetime, timezone

from cognee_community_connector_zoho_desk.zoho_desk import (
    ZohoDeskAPIError,
    ZohoDeskAuthError,
    ZohoDeskRateLimitError,
)

ORG = "60001234567"
SUPPORT = "100000000000001"
BILLING = "100000000000002"
AGENT = {"firstName": "Alice", "lastName": "Smith", "email": "alice@acme.test"}


def iso(ms: int) -> str:
    return (
        datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.")
        + f"{ms % 1000:03d}Z"
    )


def _ms(text: str) -> int:
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)


def fail(status: int):
    if status == 401:
        raise ZohoDeskAuthError("HTTP 401", 401)
    if status == 429:
        raise ZohoDeskRateLimitError("HTTP 429")
    raise ZohoDeskAPIError(f"HTTP {status}", status)


class FakeZohoDesk:
    def __init__(self, page_size: int = 50, first_index: int = 0):
        self.now = 1_760_000_000_000
        self.page_size = page_size
        self.first_index = first_index  # some Zoho listings count from 1
        self.org_id: str | None = None
        self.orgs = [{"id": ORG, "companyName": "Acme"}]
        self.departments = {SUPPORT: "Support", BILLING: "Billing"}
        self.tickets: dict[str, dict] = {}
        self.threads: dict[str, list[dict]] = {}
        self.comments: dict[str, list[dict]] = {}
        self.articles: dict[str, dict] = {}
        self.bin: list[dict] = []
        self.calls: list[str] = []
        self.fail: dict[str, int] = {}
        self.credits_left: int | None = 5000
        self.search_lag: set[str] = set()  # tickets the search index has not caught up with
        self._next = 0

    # -- building the account ------------------------------------------------
    def _id(self) -> str:
        self._next += 1
        return str(200000000000000 + self._next)

    def tick(self) -> int:
        self.now += 2000
        return self.now

    def ticket(
        self,
        subject: str,
        text: str = "My invoice is wrong.",
        customer: str = "Jane Doe",
        department: str = SUPPORT,
    ) -> str:
        tid = self._id()
        created = self.tick()
        self.tickets[tid] = {
            "id": tid,
            "ticketNumber": str(100 + self._next),
            "subject": subject,
            "description": f"<div>{text}</div>",
            "status": "Open",
            "priority": "High",
            "channel": "Email",
            "departmentId": department,
            "createdTime": iso(created),
            "modifiedTime": iso(created),
            "webUrl": f"https://desk.zoho.in/support/acme/ShowHomePage.html#Cases/dv/{tid}",
            "contact": {
                "firstName": customer.split()[0],
                "lastName": customer.split()[-1],
                "email": "jane@example.com",
                "phone": "+15550100",
            },
            "assignee": AGENT,
        }
        self.threads[tid] = []
        self.comments[tid] = []
        self.thread(tid, text, direction="in", author=customer, description=True)
        return tid

    def _touch(self, tid: str) -> None:
        self.tickets[tid]["modifiedTime"] = iso(self.tick())

    def thread(
        self,
        tid: str,
        text: str,
        direction: str = "out",
        author: str = "Alice Smith",
        private: bool = False,
        description: bool = False,
    ) -> str:
        th = self._id()
        self.threads[tid].append(
            {
                "id": th,
                "type": "thread",
                "direction": direction,
                "visibility": "private" if private else "public",
                "isDescriptionThread": description,
                "createdTime": iso(self.tick()),
                "author": {"name": author, "email": "someone@example.com"},
                "summary": text[:20],
                "content": f"<div>{text}</div>",
            }
        )
        self._touch(tid)
        return th

    def comment(self, tid: str, text: str, public: bool = False) -> str:
        cid = self._id()
        self.comments[tid].append(
            {
                "id": cid,
                "type": "comment",
                "isPublic": public,
                "commentedTime": iso(self.tick()),
                "commenter": {"name": "Alice Smith", "email": "alice@acme.test"},
                "content": text,
            }
        )
        self._touch(tid)
        return cid

    def edit_comment(self, tid: str, cid: str, text: str) -> None:
        next(c for c in self.comments[tid] if c["id"] == cid)["content"] = text
        self._touch(tid)

    def delete_ticket(self, tid: str) -> None:
        self.tickets.pop(tid)
        self.bin.append({"id": tid, "type": "Tickets", "deletedTime": iso(self.tick())})

    def purge_bin(self) -> None:
        self.bin = []

    def move(self, tid: str, department: str) -> None:
        self.tickets[tid]["departmentId"] = department
        self._touch(tid)

    def article(
        self, title: str, answer: str, status: str = "Published", department=SUPPORT
    ) -> str:
        aid = self._id()
        self.articles[aid] = {
            "id": aid,
            "title": title,
            "summary": answer[:20],
            "answer": f"<p>{answer}</p>",
            "status": status,
            "departmentId": department,
            "modifiedTime": iso(self.tick()),
            "category": {"name": "Billing FAQ"},
            "webUrl": f"https://desk.zoho.in/portal/acme/kb/articles/{aid}",
        }
        return aid

    def edit_article(self, aid: str, **changes) -> None:
        self.articles[aid].update(changes, modifiedTime=iso(self.tick()))

    def delete_article(self, aid: str) -> None:
        # Seen live: a deleted article goes to the Knowledge Base's own trash, not the
        # Recycle Bin. It drops out of the listing, and GET still answers 200 with
        # status "Published" and isTrashed true.
        self.articles[aid]["isTrashed"] = True

    def purge_article(self, aid: str) -> None:
        self.articles.pop(aid)

    # -- the API ---------------------------------------------------------------
    def _page(self, items: list[dict], params: dict) -> dict:
        start = int(params.get("from", 0))
        if start < self.first_index:
            fail(422)
        limit = min(int(params.get("limit", 50)), self.page_size)
        page = items[start - self.first_index : start - self.first_index + limit]
        return {"data": [dict(i) for i in page]} if page else {}

    def get(self, path: str, params: dict | None = None) -> tuple[dict, int]:
        params = params or {}
        self.calls.append(path)
        if len(self.calls) > 3000:
            raise AssertionError("runaway: more than 3000 calls")
        if path in self.fail:
            fail(self.fail[path])
        if path != "/organizations" and self.org_id != ORG:
            fail(422)  # orgId header missing
        parts = path.strip("/").split("/")
        now = self.now
        if parts == ["organizations"]:
            return {"data": list(self.orgs)}, now
        if parts == ["departments"]:
            items = [{"id": k, "name": v} for k, v in self.departments.items()]
            return self._page(items, params), now
        if parts == ["tickets"]:
            items = sorted(self.tickets.values(), key=lambda t: t["createdTime"])
            wanted = set(str(params.get("fields", "")).split(",")) | {"id"}
            items = [{k: v for k, v in t.items() if k in wanted} for t in items]
            return self._page(items, params), now
        if parts == ["tickets", "search"]:
            low, high = (_ms(x) for x in params["modifiedTimeRange"].split(","))
            items = sorted(
                (
                    {k: t[k] for k in ("id", "subject", "status", "departmentId", "modifiedTime")}
                    for t in self.tickets.values()
                    if low <= _ms(t["modifiedTime"]) <= high and t["id"] not in self.search_lag
                ),
                key=lambda t: t["modifiedTime"],
            )
            return self._page(items, params), now
        if parts[0] == "tickets":
            tid = parts[1]
            if tid not in self.tickets:
                fail(404)
            if len(parts) == 2:
                ticket = dict(self.tickets[tid])
                include = str(params.get("include", ""))
                if "contacts" not in include:
                    ticket.pop("contact")
                if "assignee" not in include:
                    ticket.pop("assignee")
                return ticket, now
            if parts[2] == "conversations":
                items = sorted(
                    [{k: v for k, v in t.items() if k != "content"} for t in self.threads[tid]]
                    + self.comments[tid],
                    key=lambda c: c["id"],
                    reverse=True,
                )
                return self._page(items, params), now
            if parts[2] == "threads" and len(parts) == 4:
                found = next((t for t in self.threads[tid] if t["id"] == parts[3]), None)
                if found is None:
                    fail(404)
                return dict(found), now
        if parts == ["articles"]:
            live = [a for a in self.articles.values() if not a.get("isTrashed")]
            items = sorted(live, key=lambda a: a["modifiedTime"], reverse=True)
            items = [{k: v for k, v in a.items() if k != "answer"} for a in items]
            return self._page(items, params), now
        if parts[0] == "articles" and len(parts) == 2:
            if parts[1] not in self.articles:
                fail(404)
            return dict(self.articles[parts[1]]), now
        if parts == ["recycleBin"]:
            return self._page(list(self.bin), params), now
        raise AssertionError(f"unexpected path {path}")
