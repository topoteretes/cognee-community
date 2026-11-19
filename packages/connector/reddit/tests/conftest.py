"""In-memory stand-in for Reddit's OAuth API, shared by the test modules.

Its answers are built from the recorded responses in ``fixtures/`` (see the README
there): posts and comments carry every field a real one does, ``/comments/<id>``
answers two listings (the post, then the comments), comments without replies have
``replies: ""``, replies deeper than the requested ``depth`` become "continue this
thread" stubs, ``/api/morechildren`` answers a flat list with ``parent_id``, and
``/api/info`` leaves out what it cannot find. Like Reddit it rejects a ``limit``
over 100 and more than 100 ids a request, and a ``before`` anchor that left the
listing finds nothing.
"""

import copy
import itertools
import json
import pathlib
import re

import pytest

from cognee_community_connector_reddit.reddit import (
    RedditAPIError,
    RedditAuthError,
    RedditRateLimitedError,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["response"]


_RECORDED_THREAD = fixture("comments")
POST_TEMPLATE = _RECORDED_THREAD[0]["data"]["children"][0]["data"]
COMMENT_TEMPLATE = {
    **_RECORDED_THREAD[1]["data"]["children"][0]["data"],
    "replies": "",
}
LISTING_ENVELOPE = dict.fromkeys(_RECORDED_THREAD[0]["data"])


def listing(children, after=None):
    data = {**LISTING_ENVELOPE, "modhash": "", "children": children, "after": after}
    return {"kind": "Listing", "data": data}


class FakeReddit:
    """Implements ``get`` and ``post`` like Reddit's OAuth API."""

    def __init__(self):
        self.subs: dict[str, dict] = {}
        self.calls: list[tuple[str, str, dict]] = []
        self.errors: dict[str, int] = {}
        self.rate_limited = False
        self.revoke_after: int | None = None
        self.bad_secret = False
        self.info_drops: set[str] = set()
        self.now = 1_793_000_000.0
        self._ids = itertools.count(1)

    # -- setting the site up -------------------------------------------------
    def add_sub(self, name):
        self.subs[name.lower()] = {"name": name, "posts": {}}
        return name

    def add_post(self, sub, title, body="", *, author="alice", flair=None, link=None, at=None):
        sub = self.subs.get(sub.lower()) or self.subs[self.add_sub(sub).lower()]
        post_id = f"p{next(self._ids):05d}"
        self.now += 60
        slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:40]
        post = {
            **copy.deepcopy(POST_TEMPLATE),
            "id": post_id,
            "name": f"t3_{post_id}",
            "title": title,
            "selftext": "" if link else body,
            "author": author,
            "subreddit": sub["name"],
            "subreddit_name_prefixed": f"r/{sub['name']}",
            "created_utc": self.now if at is None else at,
            "num_comments": 0,
            "edited": False,
            "is_self": link is None,
            "url": link or f"https://www.reddit.com/r/{sub['name']}/comments/{post_id}/{slug}/",
            "permalink": f"/r/{sub['name']}/comments/{post_id}/{slug}/",
            "link_flair_text": flair,
            "score": 1,
            "ups": 1,
        }
        post.setdefault("removed_by_category", None)
        sub["posts"][post_id] = {"data": post, "comments": []}
        return post

    def _thread(self, post_id):
        for sub in self.subs.values():
            if post_id in sub["posts"]:
                return sub["posts"][post_id]
        raise KeyError(post_id)

    def comment(self, post, body, *, author="bob", parent=None):
        thread = self._thread(post["id"])
        comment_id = f"c{next(self._ids):05d}"
        node = {
            **copy.deepcopy(COMMENT_TEMPLATE),
            "id": comment_id,
            "name": f"t1_{comment_id}",
            "body": body,
            "author": author,
            "link_id": f"t3_{post['id']}",
            "parent_id": parent["name"] if parent else f"t3_{post['id']}",
            "replies_list": [],
        }
        (parent["replies_list"] if parent else thread["comments"]).append(node)
        post["num_comments"] += 1
        return node

    def edit(self, post, **fields):
        post.update(fields)
        post["edited"] = self.now = self.now + 60

    def remove(self, post, how="moderator"):
        """Removed by a moderator or Reddit, or deleted by its author (``deleted``)."""
        post["removed_by_category"] = how
        # A link post has no text; only removed_by_category tells it was removed.
        if post["is_self"]:
            post["selftext"] = "[deleted]" if how == "deleted" else "[removed]"
        if how == "deleted":
            post["author"] = "[deleted]"

    def purge(self, post):
        """Gone entirely: neither listed nor returned by /api/info."""
        for sub in self.subs.values():
            sub["posts"].pop(post["id"], None)

    # -- the API --------------------------------------------------------------
    def _record(self, method, path, params):
        self.calls.append((method, path, dict(params or {})))
        if self.bad_secret:
            raise RedditAuthError("Reddit rejected the app credentials (HTTP 401)")
        if self.revoke_after is not None and len(self.calls) > self.revoke_after:
            raise RedditAuthError("Reddit rejected the token (HTTP 401)")
        if self.rate_limited:
            raise RedditRateLimitedError("Reddit rate limit exceeded (HTTP 429)")
        for key, status in self.errors.items():
            if key in path:
                raise RedditAPIError(f"Reddit request failed: HTTP {status}", status=status)

    def get(self, path, params=None):
        params = dict(params or {})
        self._record("GET", path, params)
        if int(params.get("limit", 25)) > 100 and "/new" in path:
            raise RedditAPIError("Reddit request failed: HTTP 400", status=400)
        if match := re.fullmatch(r"/r/([^/]+)/new", path):
            return self._new(match.group(1), params)
        if match := re.fullmatch(r"/comments/([^/]+)", path):
            return self._comments(match.group(1), params)
        if path == "/api/info":
            return self._info(params)
        raise RedditAPIError("Reddit request failed: HTTP 404", status=404)

    def post(self, path, data):
        self._record("POST", path, data)
        if path != "/api/morechildren":
            raise RedditAPIError("Reddit request failed: HTTP 404", status=404)
        ids = data["children"].split(",")
        if len(ids) > 100 or data.get("api_type") != "json":
            raise RedditAPIError("Reddit request failed: HTTP 400", status=400)
        thread = self._thread(data["link_id"][3:])
        wanted = set(ids)
        things = []

        def walk(nodes):
            for node in nodes:
                if node["id"] in wanted:
                    things.append({"kind": "t1", "data": self._comment_data(node, None)})
                    flat(node["replies_list"])
                else:
                    walk(node["replies_list"])

        def flat(nodes):
            for node in nodes:
                things.append({"kind": "t1", "data": self._comment_data(node, None)})
                flat(node["replies_list"])

        walk(thread["comments"])
        return {"json": {"errors": [], "data": {"things": things}}}

    def _visible(self, sub):
        posts = [p["data"] for p in sub["posts"].values() if not p["data"]["removed_by_category"]]
        return sorted(posts, key=lambda p: p["created_utc"], reverse=True)

    def _new(self, name, params):
        sub = self.subs.get(name.lower())
        if sub is None:
            # No such subreddit: Reddit redirects to its search.
            raise RedditAPIError("Reddit request failed: HTTP 302", status=302)
        posts = self._visible(sub)
        names = [p["name"] for p in posts]
        limit = int(params.get("limit", 25))
        if "before" in params:
            if params["before"] not in names:
                return listing([])
            page = posts[: names.index(params["before"])][-limit:]
        elif "after" in params:
            start = names.index(params["after"]) + 1 if params["after"] in names else len(names)
            page = posts[start : start + limit]
        else:
            page = posts[:limit]
        last = page[-1]["name"] if page else None
        more = last is not None and names.index(last) < len(names) - 1
        children = [{"kind": "t3", "data": copy.deepcopy(p)} for p in page]
        return listing(children, after=last if more and "before" not in params else None)

    def _comment_data(self, node, replies):
        data = {k: v for k, v in node.items() if k != "replies_list"}
        data["replies"] = replies if replies is not None else ""
        return copy.deepcopy(data)

    def _tree(self, nodes, depth, max_depth, budget):
        children = []
        for index, node in enumerate(nodes):
            if budget[0] <= 0:
                rest = [n["id"] for n in nodes[index:]]
                children.append(
                    {
                        "kind": "more",
                        "data": {
                            "count": len(rest),
                            "name": f"t1_{rest[0]}",
                            "id": rest[0],
                            "parent_id": node["parent_id"],
                            "depth": depth,
                            "children": rest,
                        },
                    }
                )
                break
            budget[0] -= 1
            replies = None
            if node["replies_list"]:
                if depth + 1 >= max_depth:
                    stub = copy.deepcopy(fixture("continue_thread"))
                    stub["data"].update(parent_id=node["name"], depth=depth + 1)
                    replies = listing([stub])
                else:
                    sub = self._tree(node["replies_list"], depth + 1, max_depth, budget)
                    replies = listing(sub)
            children.append({"kind": "t1", "data": self._comment_data(node, replies)})
        return children

    def _comments(self, post_id, params):
        try:
            thread = self._thread(post_id)
        except KeyError:
            raise RedditAPIError("Reddit request failed: HTTP 404", status=404) from None
        budget = [int(params.get("limit", 200))]
        tree = self._tree(thread["comments"], 0, int(params.get("depth", 10)), budget)
        post = listing([{"kind": "t3", "data": copy.deepcopy(thread["data"])}])
        return [post, listing(tree)]

    def _info(self, params):
        ids = [i for i in params.get("id", "").split(",") if i]
        if len(ids) > 100:
            raise RedditAPIError("Reddit request failed: HTTP 400", status=400)
        children = []
        for fullname in ids:
            if fullname in self.info_drops:
                continue
            try:
                post = self._thread(fullname[3:])["data"]
            except KeyError:
                continue
            children.append({"kind": "t3", "data": copy.deepcopy(post)})
        return listing(children)

    # -- helpers for assertions ------------------------------------------------
    def paths(self, prefix=""):
        return [path for _method, path, _params in self.calls if path.startswith(prefix)]


@pytest.fixture
def reddit():
    fake = FakeReddit()
    fake.add_sub("Python")
    return fake
