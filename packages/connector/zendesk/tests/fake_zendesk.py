"""An in-memory Zendesk for tests (no credentials, no network).

Its responses follow what a real trial account returned:

  - the incremental ticket export returns a ticket again when only a comment
    was added, and deleted tickets come back with ``status="deleted"``
  - comments carry a ``public`` flag (internal notes are ``public=False``), and
    ``include=users`` only sideloads the people who wrote a comment
  - archived Help Center articles disappear from the listing; drafts are flagged;
    an offset request reports ``count``, cursor pages (``page[size]``) report
    ``meta.has_more`` and ``links.next`` instead
"""

import urllib.parse

from cognee_community_connector_zendesk.zendesk import (
    ZendeskAPIError,
    ZendeskAuthError,
    ZendeskRateLimitError,
)

SUB = "acme"
ALICE = {"id": 11, "name": "Alice Customer", "email": "alice@example.com"}
AGENT = {"id": 22, "name": "Sam Agent", "email": "sam@acme.test"}


def ticket(tid, subject="Login fails", status="open", **extra):
    return {
        "id": tid,
        "subject": subject,
        "status": status,
        "priority": "high",
        "type": "problem",
        "tags": ["sso", "login"],
        "requester_id": 11,
        "assignee_id": 22,
        "created_at": "2026-10-05T10:00:00Z",
        "updated_at": "2026-10-05T10:00:00Z",
        **extra,
    }


def comment(cid, body, author=11, public=True, at="2026-10-05T10:00:00Z"):
    return {
        "id": cid,
        "plain_body": body,
        "body": body,
        "author_id": author,
        "public": public,
        "created_at": at,
    }


def article(
    aid,
    title="Reset your password",
    body="<p>Go to <b>Settings</b>.</p>",
    updated="2026-10-05T12:00:00Z",
    draft=False,
    locale="en-us",
):
    return {
        "id": aid,
        "title": title,
        "body": body,
        "updated_at": updated,
        "draft": draft,
        "html_url": f"https://{SUB}.zendesk.com/hc/{locale}/articles/{aid}",
        "locale": locale,
        "label_names": ["account"],
    }


def fail(status):
    """The error the real client raises for an HTTP status."""
    if status in (401,):
        raise ZendeskAuthError(f"HTTP {status}", status)
    if status == 429:
        raise ZendeskRateLimitError("HTTP 429", 120)
    if status == "transport":
        raise ZendeskAPIError("no connection", transport=True)
    raise ZendeskAPIError(f"HTTP {status}", status)


class FakeZendesk:
    """Serves the export as a change log: each ``touch`` appends a ticket version."""

    def __init__(self):
        self.log = []  # ticket versions in change order
        self.comments = {}  # ticket id -> comments
        self.articles = []
        self.article_page_size = 100
        self.fail_articles = False  # True, or an HTTP status to fail the listing with
        self.fail_comments = {}  # ticket id -> HTTP status its comments request fails with
        self.fail_tickets = {}  # ticket id -> HTTP status GET /tickets/{id} fails with
        self.fail_export = None  # HTTP status the export fails with
        self.fail_users = None  # HTTP status show_many fails with
        self.stall_export = False  # the export hands back the same cursor forever
        self.stall_articles = False  # article pages link to themselves forever
        self.stall_comments = False  # comment pages link to themselves forever
        self.export_without_cursor = False  # export pages carry no after_cursor
        self.blank_tickets = set()  # ticket ids GET /tickets/{id} answers without a ticket
        self.calls = []
        self.page_size = 1000

    def touch(self, t, comments=None):
        self.log.append(dict(t))
        if comments is not None:
            self.comments[t["id"]] = comments

    def _current(self, tid):
        versions = [t for t in self.log if t["id"] == tid]
        return versions[-1] if versions else None

    def get(self, path, **params):
        if path.startswith("https://"):
            url = urllib.parse.urlparse(path)
            assert url.netloc == f"{SUB}.zendesk.com", url.netloc
            path = url.path
            params = {**{k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}, **params}
        self.calls.append((path, params))
        if len(self.calls) > 300:
            raise AssertionError("FakeZendesk: more than 300 calls, the sync is looping")
        if path == "/api/v2/incremental/tickets/cursor.json":
            if self.fail_export:
                fail(self.fail_export)
            start = int(params["cursor"]) if "cursor" in params else 0
            if self.stall_export:
                return {"tickets": [], "after_cursor": str(start), "end_of_stream": False}
            page = self.log[start : start + self.page_size]
            end = start + len(page)
            return {
                "tickets": page,
                "after_cursor": None if self.export_without_cursor else str(end),
                "end_of_stream": end >= len(self.log),
                "end_time": 1_760_000_000,
            }
        if path.startswith("/api/v2/tickets/") and path.endswith("/comments.json"):
            tid = int(path.split("/")[4])
            if tid in self.fail_comments:
                fail(self.fail_comments[tid])
            comments = self.comments.get(tid, [])
            authors = {c["author_id"] for c in comments}
            # include=users only sideloads the people who wrote a comment
            return {
                "comments": comments,
                "users": [u for u in (ALICE, AGENT) if u["id"] in authors],
                "next_page": (
                    f"https://{SUB}.zendesk.com/api/v2/tickets/{tid}/comments.json?page=2"
                    if self.stall_comments
                    else None
                ),
            }
        if path.startswith("/api/v2/tickets/") and path.endswith(".json"):
            tid = int(path.split("/")[4].removesuffix(".json"))
            if tid in self.fail_tickets:
                fail(self.fail_tickets[tid])
            if tid in self.blank_tickets:
                return {"ticket": None}
            current = self._current(tid)
            if current is None or current.get("status") == "deleted":
                fail(404)
            return {"ticket": current}
        if path == "/api/v2/users/show_many.json":
            if self.fail_users:
                fail(self.fail_users)
            ids = {int(i) for i in params["ids"].split(",")}
            return {"users": [u for u in (ALICE, AGENT) if u["id"] in ids]}
        if path.startswith("/api/v2/help_center/") and path.endswith("articles.json"):
            if self.fail_articles:
                fail(500 if self.fail_articles is True else self.fail_articles)
            if "per_page" in params:  # offset pagination: reports the total
                size = int(params["per_page"])
                return {"articles": self.articles[:size], "count": len(self.articles)}
            size = min(int(params.get("page[size]", 100)), self.article_page_size)
            start = int(params.get("page[after]", 0))
            page = self.articles[start : start + size]
            more = start + size < len(self.articles)
            after = start if self.stall_articles else start + size
            return {
                "articles": page,
                "meta": {"has_more": more},
                "links": {
                    "next": (
                        f"https://{SUB}.zendesk.com/api/v2/help_center/articles.json"
                        f"?page%5Bsize%5D={size}&page%5Bafter%5D={after}"
                    )
                    if more
                    else None
                },
            }
        raise AssertionError(path)
