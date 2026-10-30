"""An in-memory Freshdesk v2 API for tests, served through a fake HTTP session.

The connector's real ``FreshdeskClient`` runs on top of it, so paging, retries
and rate-limit handling are exercised too.  The behaviour mirrors what a live
trial helpdesk did (2026-10-17):

* ``updated_since`` is inclusive and the list is ordered by ``updated_at``.
* A reply or note moves the ticket's ``updated_at``.
* Deleted and spam tickets drop out of the normal listing (even with
  ``updated_since``) and show up under ``filter=deleted`` / ``filter=spam``.
* A category lists top-level folders only; subfolders need their own call.
* Drafts are returned with ``status: 1``; a deleted article answers 404.
* Pages are linked with a ``link: <...>; rel="next"`` header, and
  ``X-Ratelimit-Remaining`` sometimes arrives as ``"38.0"``.
"""

import itertools
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlparse

HOST = "acme.freshdesk.com"
BASE = f"https://{HOST}/api/v2"


class FakeResponse:
    def __init__(self, status_code: int, body=None, headers: dict | None = None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def json(self):
        return self._body


class FakeFreshdesk:
    """Holds the helpdesk and answers ``session.get(url, params=..., ...)``."""

    def __init__(self, page_size: int | None = None):
        self.page_size = page_size  # None = honour per_page like Freshdesk
        self.clock = datetime(2026, 10, 16, 20, 0, 0)
        self.tickets: dict[int, dict] = {}
        self.conversations: dict[int, list[dict]] = {}
        self.categories: dict[int, dict] = {}
        self.folders: dict[int, dict] = {}
        self.articles: dict[int, dict] = {}
        self.fail_next: list[int] = []
        self.rate_headers: dict = {"X-Ratelimit-Total": "50", "X-Ratelimit-Remaining": "38.0"}
        self.calls: list[tuple[str, dict]] = []
        self.auth_seen = None
        self._ids = itertools.count(1)
        self._big_ids = itertools.count(87000000001)

    # ------------------------------------------------------------------
    # Helpers that change the helpdesk
    # ------------------------------------------------------------------
    def _now(self, tick: bool = True) -> str:
        if tick:
            self.clock += timedelta(seconds=1)
        return self.clock.strftime("%Y-%m-%dT%H:%M:%SZ")

    def put_ticket(self, subject: str, description: str = "", *, same_second=False, **fields):
        ticket_id = fields.pop("id", None) or next(self._ids)
        stamp = self._now(tick=not same_second)
        self.tickets[ticket_id] = {
            "id": ticket_id,
            "subject": subject,
            "description_text": description,
            "status": 2,
            "priority": 1,
            "source": 3,
            "type": None,
            "tags": [],
            "spam": False,
            "requester": {"id": 900, "name": "Meera Joshi", "email": "meera@example.com"},
            "created_at": stamp,
            "updated_at": stamp,
            **fields,
        }
        self.conversations[ticket_id] = []
        return ticket_id

    def edit_ticket(self, ticket_id: int, **fields) -> None:
        self.tickets[ticket_id].update(fields, updated_at=self._now())

    def reply(self, ticket_id: int, body: str, *, private=False, incoming=False) -> None:
        stamp = self._now()
        self.conversations[ticket_id].append(
            {
                "id": next(self._big_ids),
                "ticket_id": ticket_id,
                "body_text": body,
                "private": private,
                "incoming": incoming,
                "source": 2 if private else 0,
                "created_at": stamp,
                "updated_at": stamp,
            }
        )
        # Measured: the ticket's updated_at moves when a reply or note is added.
        self.tickets[ticket_id]["updated_at"] = self._now()

    def delete_ticket(self, ticket_id: int) -> None:
        self.tickets[ticket_id].update(deleted=True, updated_at=self._now())

    def restore_ticket(self, ticket_id: int) -> None:
        self.tickets[ticket_id].pop("deleted", None)
        self.tickets[ticket_id]["updated_at"] = self._now()

    def mark_spam(self, ticket_id: int) -> None:
        self.tickets[ticket_id].update(spam=True, updated_at=self._now())

    def purge_ticket(self, ticket_id: int) -> None:
        """Delete forever (from Trash): gone from every listing, filters included."""
        del self.tickets[ticket_id]
        del self.conversations[ticket_id]

    def add_category(self, name: str) -> int:
        category_id = next(self._big_ids)
        self.categories[category_id] = {"id": category_id, "name": name}
        return category_id

    def add_folder(self, name: str, *, category: int, parent: int | None = None) -> int:
        folder_id = next(self._big_ids)
        self.folders[folder_id] = {
            "id": folder_id,
            "name": name,
            "category_id": category,
            "parent_folder_id": parent,
        }
        return folder_id

    def put_article(self, title: str, text: str, *, folder: int, status: int = 2, **fields):
        article_id = fields.pop("id", None) or next(self._big_ids)
        self.articles[article_id] = {
            "id": article_id,
            "title": title,
            # Measured: description_text comes with surrounding spaces.
            "description_text": f" {text} ",
            "status": status,
            "folder_id": folder,
            "category_id": self.folders[folder]["category_id"],
            "tags": [],
            "updated_at": self._now(),
            **fields,
        }
        return article_id

    def edit_article(self, article_id: int, **fields) -> None:
        if "text" in fields:
            fields["description_text"] = f" {fields.pop('text')} "
        self.articles[article_id].update(fields, updated_at=self._now())

    def delete_article(self, article_id: int) -> None:
        del self.articles[article_id]

    def delete_folder(self, folder_id: int) -> None:
        for article_id in [a for a, v in self.articles.items() if v["folder_id"] == folder_id]:
            del self.articles[article_id]
        for sub in [f for f, v in self.folders.items() if v["parent_folder_id"] == folder_id]:
            self.delete_folder(sub)
        del self.folders[folder_id]

    # ------------------------------------------------------------------
    # The HTTP session surface used by FreshdeskClient
    # ------------------------------------------------------------------
    def get(self, url, params=None, auth=None, headers=None, timeout=None):
        assert timeout, "every request needs a timeout"
        self.auth_seen = auth
        parsed = urlparse(url)
        query = dict(parse_qsl(parsed.query)) if params is None else dict(params)
        path = parsed.path.removeprefix("/api/v2")
        self.calls.append((path, query))
        if self.fail_next:
            status = self.fail_next.pop(0)
            extra = {"Retry-After": "7"} if status == 429 else {}
            return FakeResponse(status, {"code": "error"}, {**self.rate_headers, **extra})
        status, items = self._route(path, query)
        if status != 200:
            return FakeResponse(status, None, dict(self.rate_headers))
        return self._page(path, query, items)

    def _route(self, path: str, query: dict) -> tuple[int, list[dict] | None]:
        parts = path.strip("/").split("/")
        if parts == ["tickets"]:
            return 200, self._list_tickets(query)
        if len(parts) == 3 and parts[0] == "tickets" and parts[2] == "conversations":
            ticket_id = int(parts[1])
            if ticket_id not in self.tickets:
                return 404, None
            return 200, list(self.conversations[ticket_id])
        if parts == ["solutions", "categories"]:
            return 200, list(self.categories.values())
        if len(parts) == 4 and parts[:2] == ["solutions", "categories"] and parts[3] == "folders":
            category_id = int(parts[2])
            if category_id not in self.categories:
                return 404, None
            return 200, [
                self._folder_json(f)
                for f in self.folders.values()
                if f["category_id"] == category_id and f["parent_folder_id"] is None
            ]
        if len(parts) == 4 and parts[:2] == ["solutions", "folders"]:
            folder_id = int(parts[2])
            if folder_id not in self.folders:
                return 404, None
            if parts[3] == "subfolders":
                return 200, [
                    self._folder_json(f)
                    for f in self.folders.values()
                    if f["parent_folder_id"] == folder_id
                ]
            if parts[3] == "articles":
                return 200, [
                    self._article_json(a)
                    for a in self.articles.values()
                    if a["folder_id"] == folder_id
                ]
        return 404, None

    def _list_tickets(self, query: dict) -> list[dict]:
        since = query.get("updated_since")
        name = query.get("filter")
        out = []
        for ticket in self.tickets.values():
            if since and ticket["updated_at"] < since:  # inclusive, like Freshdesk
                continue
            deleted, spam = ticket.get("deleted", False), ticket.get("spam", False)
            if name == "deleted":
                keep = deleted
            elif name == "spam":
                keep = spam and not deleted
            else:
                keep = not deleted and not spam
            if keep:
                out.append(self._ticket_json(ticket, query))
        if query.get("order_by") == "updated_at":
            out.sort(key=lambda t: (t["updated_at"], t["id"]))
        return out

    def _ticket_json(self, ticket: dict, query: dict) -> dict:
        includes = set(filter(None, query.get("include", "").split(",")))
        body = {k: v for k, v in ticket.items() if k not in ("requester", "description_text")}
        if "requester" in includes:
            body["requester"] = ticket["requester"]
        if "description" in includes:
            body["description_text"] = ticket["description_text"]
        return body

    def _folder_json(self, folder: dict) -> dict:
        return {
            **folder,
            "sub_folders_count": sum(
                1 for f in self.folders.values() if f["parent_folder_id"] == folder["id"]
            ),
        }

    def _article_json(self, article: dict) -> dict:
        return {**article, "hierarchy": self._hierarchy(article["folder_id"])}

    def _hierarchy(self, folder_id: int) -> list[dict]:
        chain = []
        while folder_id is not None:
            folder = self.folders[folder_id]
            chain.append(folder)
            folder_id = folder["parent_folder_id"]
        category = self.categories[chain[-1]["category_id"]]
        levels = [
            {
                "level": 0,
                "type": "category",
                "data": {"id": category["id"], "name": category["name"]},
            }
        ]
        for depth, folder in enumerate(reversed(chain), start=1):
            levels.append(
                {
                    "level": depth,
                    "type": "folder",
                    "data": {"id": folder["id"], "name": folder["name"]},
                }
            )
        return levels

    def _page(self, path: str, query: dict, items: list[dict]) -> FakeResponse:
        per_page = self.page_size or int(query.get("per_page", 30))
        page = int(query.get("page", 1))
        chunk = items[(page - 1) * per_page : page * per_page]
        headers = dict(self.rate_headers)
        if page * per_page < len(items):
            next_query = urlencode({**query, "page": page + 1})
            headers["link"] = f'<{BASE}{path}?{next_query}>; rel="next"'
        return FakeResponse(200, chunk, headers)
