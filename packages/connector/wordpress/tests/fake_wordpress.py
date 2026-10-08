"""An in-memory WordPress REST API served through ``httpx.MockTransport``.

It implements the slice of the API the connector uses (index, types,
taxonomies, categories/tags by slug, post-type collections with
``modified_after`` / ``include`` / ``status`` / taxonomy filters / ``_embed``,
comments, and ``users/me``) with the behaviour observed on a live WordPress
6.9 site:

* ``modified_after`` compares with the site-local ``post_modified``: a value
  with an offset is converted, a naive one is read in the site's timezone;
* collections are offset-paginated with ``X-WP-Total`` / ``X-WP-TotalPages``
  (two items per page here, so paging is always exercised);
* trash and other non-public statuses need credentials (HTTP 400 for the
  ``status`` parameter, 401 for an item), and rejected credentials are served
  as anonymous;
* an anonymous comments query naming a password-protected post fails with 401;
* scheduling a draft keeps its modification time when it goes live.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx

SITE_URL = "https://blog.example.com"
API = f"{SITE_URL}/wp-json/"
PAGE_SIZE = 2
SITE_OFFSET = timedelta(hours=5, minutes=30)  # the site runs on Asia/Kolkata time


def _fmt(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S")


@dataclass
class Item:
    id: int
    type: str
    title: str
    content: str
    status: str = "publish"
    author: int = 1
    categories: list[int] = field(default_factory=list)
    tags: list[int] = field(default_factory=list)
    password: str = ""
    date: datetime = field(default_factory=lambda: datetime(2026, 1, 1, tzinfo=UTC))
    modified: datetime = field(default_factory=lambda: datetime(2026, 1, 1, tzinfo=UTC))


@dataclass
class Comment:
    id: int
    post: int
    author_name: str
    content: str
    date: datetime
    parent: int = 0
    status: str = "approved"


class FakeWordPress:
    def __init__(self, *, username: str = "editor", password: str = "abcd efgh ijkl mnop"):
        self.now = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
        self.items: dict[int, Item] = {}
        self.trash: dict[int, Item] = {}
        self.comments: dict[int, Comment] = {}
        self.next_id = 100
        self.next_comment = 500
        self.credentials = (username, password)
        self.users = {1: "Alice", 2: "Bob"}
        self.categories = {10: ("news", "News"), 11: ("engineering", "Engineering")}
        self.tags = {20: ("python", "Python")}
        self.types = {
            "post": {"name": "Posts", "rest_base": "posts", "taxonomies": ["category", "post_tag"]},
            "page": {"name": "Pages", "rest_base": "pages", "taxonomies": []},
            "product": {
                "name": "Products",
                "rest_base": "products",
                "rest_namespace": "wp/v2",
                "taxonomies": [],
            },
        }
        self.requests: list[httpx.Request] = []
        self.failures: list[tuple[str, int]] = []
        # Simulates offset pagination shifting under a concurrent deletion: the
        # first item of this sweep page is skipped once.
        self.drop_from_next_sweep: int | None = None

    # -- editing helpers -------------------------------------------------------
    def tick(self, seconds: int = 1) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now

    def add(self, type_: str, title: str, content: str, **kwargs) -> Item:
        self.next_id += 1
        stamp = self.tick()
        item = Item(self.next_id, type_, title, content, date=stamp, modified=stamp, **kwargs)
        self.items[item.id] = item
        return item

    def edit(self, item_id: int, **changes) -> None:
        item = self.items[item_id]
        for key, value in changes.items():
            setattr(item, key, value)
        item.modified = self.tick()

    def comment(self, post: int, author: str, text: str, parent: int = 0) -> Comment:
        self.next_comment += 1
        comment = Comment(self.next_comment, post, author, text, self.tick(), parent)
        self.comments[comment.id] = comment
        return comment

    def trash_item(self, item_id: int) -> None:
        self.items[item_id].status = "trash"
        self.items[item_id].modified = self.tick()

    def delete(self, item_id: int) -> None:
        self.items.pop(item_id)
        self.tick()

    def schedule_then_publish(self, item_id: int) -> None:
        """A draft scheduled in the editor going live: status moves, modified does not."""
        self.items[item_id].status = "publish"
        self.tick()

    # -- transport -------------------------------------------------------------
    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def _authenticated(self, request: httpx.Request) -> bool:
        header = request.headers.get("authorization", "")
        if not header.startswith("Basic "):
            return False
        user, _, password = base64.b64decode(header[6:]).decode().partition(":")
        return (user, password) == self.credentials

    def _json(self, payload, status=200, headers=None) -> httpx.Response:
        all_headers = {"Date": format_datetime(self.now, usegmt=True), **(headers or {})}
        return httpx.Response(status, json=payload, headers=all_headers)

    def _error(self, code: str, status: int) -> httpx.Response:
        return self._json({"code": code, "message": code, "data": {"status": status}}, status)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        # Programmed failures hit API routes only, not discovery.
        if self.failures and path.startswith("/wp-json/wp/v2/"):
            kind, status = self.failures.pop(0)
            if kind == "network":
                raise httpx.ConnectError("boom", request=request)
            return httpx.Response(status, headers={"Retry-After": "0"})

        params = dict(request.url.params)
        if path == "/wp-json/":
            return self._json({"name": "Example", "namespaces": ["oembed/1.0", "wp/v2"]})
        if not path.startswith("/wp-json/wp/v2/"):
            return self._error("rest_no_route", 404)
        route = path.removeprefix("/wp-json/wp/v2/").strip("/")
        authed = self._authenticated(request)

        if route == "types":
            return self._json(self.types)
        if route == "taxonomies":
            return self._json({"category": {"name": "Categories"}, "post_tag": {"name": "Tags"}})
        if route == "users/me":
            return self._json({"id": 1}) if authed else self._error("rest_not_logged_in", 401)
        if route in ("categories", "tags"):
            terms = self.categories if route == "categories" else self.tags
            wanted = set(params.get("slug", "").split(","))
            found = [{"id": i, "slug": s} for i, (s, _n) in terms.items() if s in wanted]
            return self._paged(found, params)
        if route == "comments":
            return self._comments(params, authed)
        for slug, spec in self.types.items():
            if route == spec["rest_base"]:
                return self._collection(slug, params, authed)
        return self._error("rest_no_route", 404)

    def _paged(self, items: list, params: dict) -> httpx.Response:
        per_page = min(int(params.get("per_page", 10)), PAGE_SIZE)
        page = int(params.get("page", 1))
        total_pages = max((len(items) + per_page - 1) // per_page, 1)
        if page > total_pages:
            return self._error("rest_post_invalid_page_number", 400)
        chunk = items[(page - 1) * per_page : page * per_page]
        headers = {"X-WP-Total": str(len(items)), "X-WP-TotalPages": str(total_pages)}
        return self._json(chunk, headers=headers)

    def _collection(self, type_: str, params: dict, authed: bool) -> httpx.Response:
        statuses = set(params.get("status", "publish").split(","))
        if statuses - {"publish"} and not authed:
            return self._error("rest_invalid_param", 400)
        items = [i for i in self.items.values() if i.type == type_ and i.status in statuses]
        if params.get("include"):
            wanted = {int(x) for x in params["include"].split(",")}
            items = [i for i in items if i.id in wanted]
        if params.get("categories"):
            wanted = {int(x) for x in params["categories"].split(",")}
            items = [i for i in items if wanted & set(i.categories)]
        if params.get("tags"):
            wanted = {int(x) for x in params["tags"].split(",")}
            items = [i for i in items if wanted & set(i.tags)]
        if params.get("modified_after"):
            raw = params["modified_after"]
            after = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if after.tzinfo is None:  # naive: read in the site's timezone
                after = after.replace(tzinfo=UTC) - SITE_OFFSET
            items = [i for i in items if i.modified > after]
        by_modified = params.get("orderby") == "modified"
        items.sort(
            key=lambda i: (i.modified, i.id) if by_modified else (i.id,),
            reverse=params.get("order") == "desc",
        )

        is_sweep = params.get("_fields") == "id,modified_gmt"
        if is_sweep and self.drop_from_next_sweep in [i.id for i in items]:
            items = [i for i in items if i.id != self.drop_from_next_sweep]
            self.drop_from_next_sweep = None
        return self._paged([self._render(i, params) for i in items], params)

    def _render(self, item: Item, params: dict) -> dict:
        if params.get("_fields") == "id,modified_gmt":
            return {"id": item.id, "modified_gmt": _fmt(item.modified)}
        protected = bool(item.password)
        out = {
            "id": item.id,
            "type": item.type,
            "status": item.status,
            "date_gmt": _fmt(item.date),
            "modified_gmt": _fmt(item.modified),
            "link": f"{SITE_URL}/?p={item.id}",
            "title": {"rendered": item.title},
            "content": {"rendered": "" if protected else item.content, "protected": protected},
            "excerpt": {"rendered": "", "protected": protected},
            "author": item.author,
        }
        if params.get("_embed"):
            terms = []
            spec = self.types[item.type]
            if "category" in spec["taxonomies"]:
                terms.append(
                    [
                        {"id": c, "name": self.categories[c][1], "taxonomy": "category"}
                        for c in item.categories
                    ]
                )
            if "post_tag" in spec["taxonomies"]:
                terms.append(
                    [{"id": t, "name": self.tags[t][1], "taxonomy": "post_tag"} for t in item.tags]
                )
            out["_embedded"] = {
                "author": [{"id": item.author, "name": self.users.get(item.author)}],
                "wp:term": terms,
            }
        return out

    @staticmethod
    def _public(item: Item) -> bool:
        return item.status == "publish" and not item.password

    def _comments(self, params: dict, authed: bool) -> httpx.Response:
        comments = [c for c in self.comments.values() if c.status == "approved"]
        if params.get("post"):
            wanted = {int(x) for x in params["post"].split(",")}
            if not authed and any(
                self.items.get(post_id) and self.items[post_id].password for post_id in wanted
            ):
                return self._error("rest_cannot_read_post", 401)
            comments = [c for c in comments if c.post in wanted]
        # Comments of items the caller cannot read are not listed.
        comments = [
            c
            for c in comments
            if c.post in self.items
            and (
                authed
                or (self.items[c.post].status == "publish" and not self.items[c.post].password)
            )
        ]
        comments.sort(key=lambda c: c.date if params.get("orderby") == "date_gmt" else c.id)
        if params.get("_fields") == "id,post":
            rows = [{"id": c.id, "post": c.post} for c in comments]
        else:
            rows = [
                {
                    "id": c.id,
                    "post": c.post,
                    "parent": c.parent,
                    "author_name": c.author_name,
                    "date_gmt": _fmt(c.date),
                    "content": {"rendered": f"<p>{c.content}</p>"},
                }
                for c in comments
            ]
        return self._paged(rows, params)
