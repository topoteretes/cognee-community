"""An in-memory Zendesk for tests (no credentials, no network).

Its responses follow what a real trial account returned:

  - the incremental ticket export returns a ticket again when only a comment
    was added, and deleted tickets come back with ``status="deleted"``
  - comments carry a ``public`` flag (internal notes are ``public=False``), and
    ``include=users`` only sideloads the people who wrote a comment
  - archived Help Center articles disappear from the listing; drafts are flagged
"""

from cognee_community_connector_zendesk.zendesk import ZendeskAPIError

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
):
    return {
        "id": aid,
        "title": title,
        "body": body,
        "updated_at": updated,
        "draft": draft,
        "html_url": f"https://{SUB}.zendesk.com/hc/en-us/articles/{aid}",
        "locale": "en-us",
        "label_names": ["account"],
    }


class FakeZendesk:
    """Serves the export as a change log: each ``touch`` appends a ticket version."""

    def __init__(self):
        self.log = []  # ticket versions in change order
        self.comments = {}  # ticket id -> comments
        self.articles = []
        self.fail_articles = False
        self.calls = []
        self.page_size = 1000

    def touch(self, t, comments=None):
        self.log.append(dict(t))
        if comments is not None:
            self.comments[t["id"]] = comments

    def get(self, path, **params):
        self.calls.append((path, params))
        if path == "/api/v2/incremental/tickets/cursor.json":
            start = int(params["cursor"]) if "cursor" in params else 0
            page = self.log[start : start + self.page_size]
            end = start + len(page)
            return {
                "tickets": page,
                "after_cursor": str(end),
                "end_of_stream": end >= len(self.log),
            }
        if path.startswith("/api/v2/tickets/") and path.endswith("/comments.json"):
            tid = int(path.split("/")[4])
            comments = self.comments.get(tid, [])
            authors = {c["author_id"] for c in comments}
            # include=users only sideloads the people who wrote a comment
            return {
                "comments": comments,
                "users": [u for u in (ALICE, AGENT) if u["id"] in authors],
                "next_page": None,
            }
        if path == "/api/v2/users/show_many.json":
            ids = {int(i) for i in params["ids"].split(",")}
            return {"users": [u for u in (ALICE, AGENT) if u["id"] in ids]}
        if path == "/api/v2/help_center/articles.json":
            if self.fail_articles:
                raise ZendeskAPIError("HTTP 500")
            return {"articles": list(self.articles), "next_page": None}
        raise AssertionError(path)
