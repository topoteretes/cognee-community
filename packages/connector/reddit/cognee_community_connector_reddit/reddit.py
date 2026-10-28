"""Reddit connector for cognee: a ``dlt`` source that turns subreddit threads into memory.

Sync the posts of selected subreddits, each with its comment thread, into cognee,
incrementally and with forget-on-delete. The resource is handed directly to
:func:`cognee.remember`::

    import cognee
    from cognee_community_connector_reddit import reddit_source

    await cognee.remember(
        reddit_source(
            subreddits=["python"],
            client_id=client_id,
            client_secret=client_secret,
            user_agent="script:cognee-reddit:0.1 (by /u/your_name)",
        ),
        dataset_name="reddit",
        primary_key="id",
        write_disposition="merge",  # REQUIRED, the add pipeline defaults to "replace"
        max_rows_per_table=0,
    )

Design
------
* **Rows** are flat ``{id, title, content, url, cognee_node_set, _deleted}``, one per
  post (its fullname, ``t3_<id>``): the subreddit, author, flair, date and body,
  then the comment thread as an indented tree. Vote counts are left out, so votes
  alone never re-process a post. ``cognee_node_set`` is ``["r/<subreddit>"]``.
* **New posts** come from ``/r/<sub>/new`` with the ``before`` cursor. A full page,
  or an empty one whose anchor post is gone, falls back to paging ``after`` down to
  the anchor's time, so no post is skipped.
* **Changes and deletions** of posts already synced are read from ``/api/info``,
  100 posts a request. A post is forgotten only when Reddit says it was removed or
  deleted, or no longer returns it while its subreddit answers. A post that just
  drops off a listing stays. Threads of recent posts whose comment count, text or
  edit time changed are read again.
* **Comment trees** come from the second of the two listings ``/comments/<id>``
  returns. ``more`` stubs are expanded one ``/api/morechildren`` request at a time,
  within a depth and a per-thread request limit; a thread cut short says so.
* **A run is bounded** by a request budget and Reddit's rate limit headers, and
  stops cleanly, keeping what it finished, on a spent budget, a long rate limit or
  a token revoked mid-run.

Privacy
-------
This reads public posts and comments of the selected subreddits, with their
authors' usernames. Nothing is fetched until you construct a source and call
``remember``, and anyone with read access to the target dataset can read what was
ingested. The connector only reads: it never posts, votes or messages.
"""

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("reddit_connector")

# Credentials are only ever sent to Reddit's own hosts.
_AUTH_URL = "https://www.reddit.com/api/v1/access_token"
_API_URL = "https://oauth.reddit.com"
_SITE_URL = "https://www.reddit.com"
# Reddit usually answers within a second; only a stuck connection hits this.
_TIMEOUT = 30.0
# Attempts per request; backing off 1, 2, 4 and 8 seconds gives up after about 15.
_RETRIES = 5
# A longer wait stops the run instead of holding cognee's ingestion lock.
_MAX_RETRY_SLEEP = 60.0
# Total time one run may wait on retries and rate limits before it stops cleanly.
DEFAULT_MAX_SLEEP = 300.0
# Reddit's free tier allows 100 requests a minute per OAuth client.
DEFAULT_MAX_REQUESTS = 100
# Reddit's largest page: listings and /api/info take at most 100 items a request,
# and /api/morechildren at most 100 comment ids.
_PAGE = 100
# Reddit replaces replies deeper than 10 with "continue this thread".
DEFAULT_MAX_COMMENT_DEPTH = 8
DEFAULT_MAX_MORE_REQUESTS = 5
# Comments of a post mostly arrive in its first days.
DEFAULT_REFRESH_DAYS = 7
DEFAULT_FIRST_SYNC_POSTS = 100
# The comment count /comments/<id> loads before it leaves "more" stubs.
_COMMENT_LIMIT = 500
# A token is renewed this long before it expires.
_TOKEN_MARGIN = 60.0
_SUBREDDIT = re.compile(r"[A-Za-z0-9_]{2,21}")
_RESOURCE_NAME = re.compile(r"[a-z0-9_]{1,64}")
_GONE_TEXT = ("[deleted]", "[removed]")
_UNREADABLE = (403, 404)
_STATS = (
    "scanned",
    "emitted",
    "unchanged",
    "deleted",
    "gone",
    "unreadable",
    "truncated",
    "skipped",
    "failed",
    "failed_auth",
    "failed_budget",
    "failed_rate_limit",
)


# ---------------------------------------------------------------------------
# Errors: messages carry the HTTP status only, never credentials or bodies.
# ---------------------------------------------------------------------------
class RedditSourceError(RuntimeError):
    """Base class of the errors this source raises."""


class RedditAuthError(RedditSourceError):
    """Reddit rejected the app's credentials or the token."""


class RedditRateLimitedError(RedditSourceError):
    """Reddit's rate limit asks for a long wait, or the wait budget is spent."""


class RedditAPIError(RedditSourceError):
    """Reddit answered with an error or an unexpected body, or could not be reached."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
def _clean(value: str | None, what: str) -> str:
    value = (value or "").strip()
    # Rejected without echoing the value: httpx would put it into its exception.
    if not value or not value.isascii() or not value.isprintable():
        raise ValueError(f"The Reddit {what} is empty or has an invalid format")
    return value


def _seconds(response: Any, header: str) -> float | None:
    try:
        return max(float(response.headers.get(header)), 0.0)
    except (TypeError, ValueError):
        return None


class RedditClient:
    """Minimal synchronous client for Reddit's OAuth API.

    ``get`` and ``post`` return the decoded JSON body. App-only OAuth
    (``client_credentials``) is used unless a username and password are given.
    Tests and hosts can pass any object with the same methods to
    :func:`reddit_source` instead.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        user_agent: str,
        *,
        username: str | None = None,
        password: str | None = None,
        http: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        max_sleep: float = DEFAULT_MAX_SLEEP,
        check_active: Callable[[], None] | None = None,
    ):
        import httpx

        self._auth = (_clean(client_id, "client id"), _clean(client_secret, "client secret"))
        self._user_agent = _clean(user_agent, "user agent")
        self._grant = (
            {"grant_type": "password", "username": username, "password": password}
            if username and password
            else {"grant_type": "client_credentials"}
        )
        self._owns_http = http is None
        self._http = http or httpx.Client(timeout=_TIMEOUT, follow_redirects=False)
        self._sleep = sleep
        self._clock = clock
        self._max_sleep = max_sleep
        self._check_active = check_active
        self._slept = 0.0
        self._token: str | None = None
        self._expires = 0.0
        self._remaining: float | None = None
        self._reset: float | None = None

    def __repr__(self) -> str:
        return "RedditClient(client_secret=<redacted>)"

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def _wait(self, delay: float) -> None:
        if delay > _MAX_RETRY_SLEEP or self._slept + delay > self._max_sleep:
            raise RedditRateLimitedError("Reddit asked for a longer wait than this run allows")
        self._slept += delay
        self._sleep(delay)
        if self._check_active is not None:
            self._check_active()

    def _fetch_token(self) -> None:
        import httpx

        try:
            response = self._http.post(
                _AUTH_URL,
                data=self._grant,
                auth=self._auth,
                headers={"User-Agent": self._user_agent},
            )
        except httpx.TransportError as exc:
            raise RedditAPIError(f"Reddit token request failed: {type(exc).__name__}") from exc
        if response.status_code in (400, 401, 403):
            raise RedditAuthError(
                f"Reddit rejected the app credentials (HTTP {response.status_code})"
            )
        if response.status_code != 200:
            raise RedditAPIError(
                f"Reddit token request failed: HTTP {response.status_code}",
                status=response.status_code,
            )
        try:
            body = response.json()
        except ValueError:
            raise RedditAPIError("Reddit answered a token request that is not JSON") from None
        token = body.get("access_token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            # A wrong username or password answers 200 with {"error": "invalid_grant"}.
            raise RedditAuthError("Reddit rejected the app credentials")
        self._token = token
        expires_in = body.get("expires_in")
        lifetime = float(expires_in) if isinstance(expires_in, (int, float)) else 3600.0
        self._expires = self._clock() + lifetime

    def _note_limits(self, response: Any) -> None:
        remaining = _seconds(response, "x-ratelimit-remaining")
        if remaining is not None:
            self._remaining = remaining
            self._reset = _seconds(response, "x-ratelimit-reset")

    def request(self, method: str, path: str, params=None, data=None) -> Any:
        import httpx

        if self._remaining is not None and self._remaining < 1:
            # The window is used up: wait for it to reset, or stop.
            self._wait(self._reset if self._reset is not None else _MAX_RETRY_SLEEP)
            self._remaining = None
        renewed = False
        attempt = 0
        while True:
            last = attempt == _RETRIES - 1
            if self._token is None or self._clock() >= self._expires - _TOKEN_MARGIN:
                self._fetch_token()
            try:
                response = self._http.request(
                    method,
                    _API_URL + path,
                    params=params,
                    data=data,
                    headers={
                        "Authorization": f"bearer {self._token}",
                        "User-Agent": self._user_agent,
                    },
                )
            except httpx.TransportError as exc:
                if last:
                    raise RedditAPIError(f"Reddit request failed: {type(exc).__name__}") from exc
                self._wait(float(2**attempt))
                attempt += 1
                continue
            self._note_limits(response)
            status = response.status_code
            if status == 401 and not renewed:
                # The token expired or was revoked: renew it once.
                self._token = None
                renewed = True
                continue
            if status == 401:
                raise RedditAuthError("Reddit rejected the token (HTTP 401)")
            if status == 429:
                wait = _seconds(response, "retry-after") or _seconds(response, "x-ratelimit-reset")
                if last:
                    raise RedditRateLimitedError("Reddit rate limit exceeded (HTTP 429)")
                self._wait(wait if wait is not None else float(2**attempt))
            elif status >= 500 and not last:
                self._wait(float(2**attempt))
            elif status != 200:
                raise RedditAPIError(f"Reddit request failed: HTTP {status}", status=status)
            else:
                try:
                    return response.json()
                except ValueError:
                    raise RedditAPIError("Reddit answered with a body that is not JSON") from None
            attempt += 1

    def get(self, path: str, params: dict | None = None) -> Any:
        return self.request("GET", path, params={**(params or {}), "raw_json": 1})

    def post(self, path: str, data: dict) -> Any:
        return self.request("POST", path, params={"raw_json": 1}, data=data)


# ---------------------------------------------------------------------------
# Rendering: deterministic, no volatile fields such as votes.
# ---------------------------------------------------------------------------
def _text(value: Any) -> str:
    return str(value or "").strip()


def _gone(post: dict) -> bool:
    """Whether Reddit reports the post as removed or deleted.

    A post whose author deleted their account keeps its text, so a ``[deleted]``
    author alone is not a deleted post.
    """
    return bool(post.get("removed_by_category")) or _text(post.get("selftext")) in _GONE_TEXT


def _indent(text: str, prefix: str) -> str:
    lines = text.splitlines() or [""]
    return "\n".join([lines[0]] + [prefix + "  " + line for line in lines[1:]])


def _render_comments(comments: list[dict], depth: int = 0) -> list[str]:
    lines: list[str] = []
    prefix = "  " * depth
    for comment in comments:
        replies = _render_comments(comment["replies"], depth + 1)
        body = _text(comment["body"])
        if body in _GONE_TEXT or not body:
            if not replies:
                continue
            lines.append(f"{prefix}- [deleted]")
        else:
            lines.append(f"{prefix}- u/{comment['author']}: {_indent(body, prefix)}")
        lines.extend(replies)
    return lines


def render_post(post: dict, comments: list[dict] | None, truncated: bool) -> dict[str, Any]:
    subreddit = _text(post.get("subreddit"))
    header = [f"Subreddit: r/{subreddit}", f"Author: u/{_text(post.get('author'))}"]
    if flair := _text(post.get("link_flair_text")):
        header.append(f"Flair: {flair}")
    created = post.get("created_utc")
    if isinstance(created, (int, float)):
        posted = datetime.fromtimestamp(created, tz=timezone.utc).strftime("%Y-%m-%d")
        header.append(f"Posted: {posted}")
    if not post.get("is_self") and (link := _text(post.get("url"))):
        header.append(f"Link: {link}")
    parts = ["\n".join(header)]
    if body := _text(post.get("selftext")):
        parts.append(body)
    if comments is not None:
        lines = _render_comments(comments)
        if lines:
            parts.append("Comments:\n" + "\n".join(lines))
        if truncated:
            parts.append("(Some comments of this thread were not loaded.)")
    return {
        "id": f"t3_{post['id']}",
        "title": _text(post.get("title")) or "Untitled post",
        "content": "\n\n".join(parts),
        "url": _SITE_URL + _text(post.get("permalink")),
        dlt_utils.NODE_SET_COLUMN: [f"r/{subreddit}"],
        "_deleted": False,
    }


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _meta(post: dict) -> str:
    """What decides whether a known post changed, besides its comments."""
    return _digest(
        [
            post.get("title"),
            post.get("selftext"),
            post.get("link_flair_text"),
            post.get("url"),
            post.get("author"),
            post.get("edited"),
            post.get("num_comments"),
        ]
    )


# ---------------------------------------------------------------------------
# Comment trees
# ---------------------------------------------------------------------------
def _listing_children(listing: Any) -> list:
    if (
        not isinstance(listing, dict)
        or listing.get("kind") != "Listing"
        or not isinstance(listing.get("data"), dict)
        or not isinstance(listing["data"].get("children"), list)
    ):
        raise RedditAPIError("Reddit answered a listing in an unexpected shape")
    return listing["data"]["children"]


class _Thread:
    """A post's comment tree, with the ``more`` stubs still to expand."""

    def __init__(self, link_id: str, max_depth: int):
        self.link_id = link_id
        self.max_depth = max_depth
        self.top: list[dict] = []
        self.nodes: dict[str, tuple[dict, int]] = {}
        self.more: list[tuple[list[str], int]] = []
        self.truncated = False

    def add(self, things: list, parent: list[dict], depth: int) -> None:
        for thing in things:
            if not isinstance(thing, dict) or not isinstance(thing.get("data"), dict):
                continue
            data = thing["data"]
            if thing.get("kind") == "more":
                ids = [c for c in data.get("children") or [] if isinstance(c, str)]
                if ids and depth <= self.max_depth:
                    self.more.append((ids, depth))
                elif data.get("count") or data.get("id") == "_" or ids:
                    # "Continue this thread", or deeper than this connector reads.
                    self.truncated = True
                continue
            if thing.get("kind") != "t1" or not isinstance(data.get("id"), str):
                continue
            if depth > self.max_depth:
                self.truncated = True
                continue
            node = {"author": _text(data.get("author")), "body": data.get("body"), "replies": []}
            parent.append(node)
            self.nodes[f"t1_{data['id']}"] = (node, depth)
            replies = data.get("replies")
            if isinstance(replies, dict):
                self.add(_listing_children(replies), node["replies"], depth + 1)

    def attach(self, things: list) -> None:
        """Place the flat ``/api/morechildren`` answer under each comment's parent."""
        for thing in things:
            data = thing.get("data") if isinstance(thing, dict) else None
            if not isinstance(data, dict):
                continue
            parent_id = data.get("parent_id")
            if parent_id == self.link_id:
                self.add([thing], self.top, 0)
            elif parent_id in self.nodes:
                node, depth = self.nodes[parent_id]
                self.add([thing], node["replies"], depth + 1)
            else:
                self.truncated = True


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _RedditConfig:
    subreddits: tuple[str, ...]
    include_comments: bool = True
    max_comment_depth: int = DEFAULT_MAX_COMMENT_DEPTH
    max_more_requests: int = DEFAULT_MAX_MORE_REQUESTS
    refresh_days: float = DEFAULT_REFRESH_DAYS
    first_sync_posts: int = DEFAULT_FIRST_SYNC_POSTS
    max_requests: int = DEFAULT_MAX_REQUESTS


class _CutShortError(Exception):
    """Internal: stop the run here, keeping the posts finished so far."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Walker:
    """One run over the selected subreddits, writing progress into ``state``.

    ``state["subs"]`` holds each subreddit's ``before`` anchor and its synced
    posts, ``state["pending"]`` the threads still to read, so a run cut short
    continues where it stopped.
    """

    def __init__(
        self,
        client: Any,
        config: _RedditConfig,
        state: dict,
        stats: dict[str, int],
        check_active: Callable[[], None] | None,
        now: float,
    ):
        self.client = client
        self.config = config
        self.state = state
        self.stats = stats
        self.check_active = check_active
        self.now = now
        self.subs: dict[str, dict] = state.setdefault("subs", {})
        self.pending: list[list[str]] = state.setdefault("pending", [])
        self.requests = 0
        # Set once a listing, a check or a thread is finished; the budget never
        # stops a run before that.
        self.progressed = False
        # The "after" cursor of the last listing page read.
        self._after: str | None = None

    def _call(self, method: str, path: str, payload: dict | None = None) -> Any:
        if self.check_active is not None:
            self.check_active()
        if self.progressed and self.requests >= self.config.max_requests:
            raise _CutShortError("budget")
        self.requests += 1
        try:
            return getattr(self.client, method)(path, payload)
        except RedditRateLimitedError:
            raise _CutShortError("rate_limit") from None
        except RedditAuthError:
            # Revoked during the run: keep what is done. On the first request it
            # is a wrong secret, which raises.
            if self.requests > 1:
                raise _CutShortError("auth") from None
            raise

    def _forget(self, key: str, post_id: str) -> Iterator[dict]:
        entry = self.subs.get(key, {}).get("posts", {}).pop(post_id, None)
        if entry and entry.get("doc"):
            self.stats["deleted"] += 1
            yield {"id": f"t3_{post_id}", "_deleted": True}

    # -- the run -----------------------------------------------------------
    def rows(self) -> Iterator[dict]:
        selected = {name.lower(): name for name in self.config.subreddits}
        # A subreddit that is no longer selected is forgotten as a whole.
        for key in [k for k in self.subs if k not in selected]:
            for post_id in list(self.subs[key].get("posts", {})):
                yield from self._forget(key, post_id)
            del self.subs[key]
        self.pending[:] = [p for p in self.pending if p[0] in selected]
        # Threads a run cut short left go first, so a small budget still drains them.
        yield from self._drain()
        for key, name in sorted(selected.items()):
            yield from self._sync(key, name)
        yield from self._drain()

    def _drain(self) -> Iterator[dict]:
        while self.pending:
            key, post_id = self.pending[0]
            yield from self._read_thread(key, post_id)
            self.pending.pop(0)
            self.progressed = True

    def _sync(self, key: str, name: str) -> Iterator[dict]:
        sub = self.subs.get(key)
        new = self._new_posts(key, name, sub)
        if new is None:
            return
        sub = self.subs.setdefault(key, {"posts": {}})
        known = sub["posts"]
        queued = {p[1] for p in self.pending if p[0] == key}
        for post in sorted(new, key=lambda p: p["created_utc"]):
            if post["id"] not in known and post["id"] not in queued:
                yield from self._take(key, post)
                queued.add(post["id"])
        if new:
            newest = max(new, key=lambda p: p["created_utc"])
            if newest["created_utc"] >= sub.get("anchor_utc", 0):
                sub["anchor"] = newest["name"]
                sub["anchor_utc"] = newest["created_utc"]
        self.progressed = True
        yield from self._check_known(key, {p["id"] for p in new})

    # -- new posts ---------------------------------------------------------
    def _listing(self, key: str, name: str, params: dict) -> list[dict] | None:
        try:
            listing = self._call("get", f"/r/{name}/new", {"limit": _PAGE, **params})
        except RedditAPIError as exc:
            # 403 private, quarantined or banned, 404 gone, 3xx no such subreddit.
            if exc.status not in _UNREADABLE and not (exc.status and 300 <= exc.status < 400):
                raise
            if key not in self.subs:
                raise RedditSourceError(
                    f"The subreddit r/{name} cannot be read (HTTP {exc.status})"
                ) from exc
            logger.warning("Reddit: r/%s answered %s, left unchanged.", name, exc.status)
            self.stats["unreadable"] += 1
            return None
        posts = []
        for child in _listing_children(listing):
            data = child.get("data") if isinstance(child, dict) else None
            if (
                child.get("kind") != "t3"
                or not isinstance(data, dict)
                or not isinstance(data.get("id"), str)
                or not isinstance(data.get("created_utc"), (int, float))
            ):
                self.stats["skipped"] += 1
                continue
            data.setdefault("name", f"t3_{data['id']}")
            self.stats["scanned"] += 1
            posts.append(data)
        self._after = listing["data"].get("after")
        return posts

    def _new_posts(self, key: str, name: str, sub: dict | None) -> list[dict] | None:
        anchor = (sub or {}).get("anchor")
        if not anchor:
            return self._page_down(key, name, None, self.config.first_sync_posts)
        posts = self._listing(key, name, {"before": anchor})
        if posts is None:
            return None
        if posts and len(posts) < _PAGE:
            return posts
        if not posts:
            # Nothing new, or the anchor post is gone and "before" finds nothing.
            top = self._listing(key, name, {"limit": 1})
            if top is None:
                return None
            if not top or max(p["created_utc"] for p in top) <= sub["anchor_utc"]:
                return []
        # More new posts than a page, or a lost anchor: page down to the anchor's time.
        return self._page_down(key, name, sub["anchor_utc"], None)

    def _page_down(
        self, key: str, name: str, since: float | None, limit: int | None
    ) -> list[dict] | None:
        posts: list[dict] = []
        params: dict = {}
        while True:
            page = self._listing(key, name, params)
            if page is None:
                return posts or None
            fresh = [p for p in page if since is None or p["created_utc"] > since]
            posts.extend(fresh)
            if limit is not None and len(posts) >= limit:
                return posts[:limit]
            if not self._after or len(fresh) < len(page) or not page:
                return posts
            params = {"after": self._after}

    # -- known posts -------------------------------------------------------
    def _check_known(self, key: str, listed: set[str]) -> Iterator[dict]:
        """Read every synced post back from /api/info, 100 at a time, from where the
        last run stopped, so each one gets its turn."""
        sub = self.subs[key]
        ids = sorted(p for p in sub["posts"] if p not in listed)
        start = sub.get("checked")
        if start:
            ids = [i for i in ids if i > start] + [i for i in ids if i <= start]
        for batch in (ids[i : i + _PAGE] for i in range(0, len(ids), _PAGE)):
            answer = self._call("get", "/api/info", {"id": ",".join(f"t3_{i}" for i in batch)})
            yield from self._reconcile(key, batch, _listing_children(answer))
            sub["checked"] = batch[-1]
            self.progressed = True
        sub.pop("checked", None)

    def _reconcile(self, key: str, batch: list[str], children: list) -> Iterator[dict]:
        found = {}
        for child in children:
            data = child.get("data") if isinstance(child, dict) else None
            if isinstance(data, dict) and isinstance(data.get("id"), str):
                found[data["id"]] = data
        if not found:
            # An answer without any of the posts is confirmed by the next run.
            empty = self.subs[key].setdefault("empty", [])
            if batch[0] not in empty:
                empty.append(batch[0])
                logger.warning("Reddit returned none of %d synced posts; kept.", len(batch))
                return
            empty.remove(batch[0])
        else:
            self.subs[key].pop("empty", None)
        posts = self.subs[key]["posts"]
        for post_id in batch:
            post = found.get(post_id)
            if post is None or _gone(post):
                self.stats["gone"] += 1
                yield from self._forget(key, post_id)
                continue
            self.stats["scanned"] += 1
            entry = posts[post_id]
            recent = self.now - float(entry.get("utc", 0)) <= self.config.refresh_days * 86400
            if entry.get("meta") == _meta(post) or not recent:
                self.stats["unchanged"] += 1
                continue
            yield from self._take(key, post)

    # -- threads -------------------------------------------------------------
    def _take(self, key: str, post: dict) -> Iterator[dict]:
        """Ingest a new or changed post: now, or once its thread is read."""
        if self.config.include_comments:
            if [key, post["id"]] not in self.pending:
                self.pending.append([key, post["id"]])
            return
        yield from self._finish(key, post, None, False)

    def _read_thread(self, key: str, post_id: str) -> Iterator[dict]:
        try:
            answer = self._call(
                "get",
                f"/comments/{post_id}",
                {"limit": _COMMENT_LIMIT, "depth": self.config.max_comment_depth + 1},
            )
        except RedditAPIError as exc:
            if exc.status == 404:
                self.stats["gone"] += 1
                yield from self._forget(key, post_id)
                return
            if exc.status != 403:
                raise
            logger.warning("Reddit: post %s answered 403, left unchanged.", post_id)
            self.stats["unreadable"] += 1
            return
        if not isinstance(answer, list) or len(answer) != 2:
            raise RedditAPIError("Reddit answered a thread that is not two listings")
        posts = [c.get("data") for c in _listing_children(answer[0]) if c.get("kind") == "t3"]
        if not posts or not isinstance(posts[0], dict) or posts[0].get("id") != post_id:
            raise RedditAPIError("Reddit answered a thread without its post")
        post = posts[0]
        if _gone(post):
            self.stats["gone"] += 1
            yield from self._forget(key, post_id)
            return
        # The comments are the second listing; the first only holds the post.
        thread = _Thread(f"t3_{post_id}", self.config.max_comment_depth)
        thread.add(_listing_children(answer[1]), thread.top, 0)
        self._expand(thread)
        yield from self._finish(key, post, thread.top, thread.truncated)

    def _expand(self, thread: _Thread) -> None:
        """Expand ``more`` stubs one request at a time, within the thread's limit."""
        used = 0
        while thread.more:
            if used >= self.config.max_more_requests:
                thread.truncated = True
                return
            ids, _depth = thread.more.pop(0)
            if len(ids) > _PAGE:
                thread.more.insert(0, (ids[_PAGE:], _depth))
                ids = ids[:_PAGE]
            used += 1
            answer = self._call(
                "post",
                "/api/morechildren",
                {
                    "api_type": "json",
                    "link_id": thread.link_id,
                    "children": ",".join(ids),
                    "sort": "confidence",
                },
            )
            body = answer.get("json") if isinstance(answer, dict) else None
            things = (body or {}).get("data", {}).get("things") if isinstance(body, dict) else None
            if not isinstance(things, list) or (body or {}).get("errors"):
                thread.truncated = True
                return
            thread.attach(things)

    def _finish(
        self, key: str, post: dict, comments: list[dict] | None, truncated: bool
    ) -> Iterator[dict]:
        row = render_post(post, comments, truncated)
        posts = self.subs.setdefault(key, {"posts": {}})["posts"]
        previous = posts.get(post["id"])
        posts[post["id"]] = {
            "utc": post.get("created_utc"),
            "meta": _meta(post),
            "hash": _digest(row),
            "doc": True,
        }
        if truncated:
            self.stats["truncated"] += 1
        if previous and previous.get("hash") == posts[post["id"]]["hash"]:
            self.stats["unchanged"] += 1
            return
        self.stats["emitted"] += 1
        yield row


def _iter_rows(
    client: Any,
    config: _RedditConfig,
    state: dict,
    stats: dict[str, int],
    check_active: Callable[[], None] | None = None,
    now: float | None = None,
) -> Iterator[dict]:
    """Yield changed rows and tombstones. Pure of dlt, so tests drive it with a dict.

    Rejected credentials raise :class:`RedditAuthError`, which is never read as "gone".
    """
    for key in _STATS:
        stats.setdefault(key, 0)
    walker = _Walker(client, config, state, stats, check_active, now or time.time())
    try:
        yield from walker.rows()
    except _CutShortError as cut:
        # A spent budget is a planned pause, not a failure; failed_budget says why.
        if cut.reason != "budget":
            stats["failed"] = 1
        stats[f"failed_{cut.reason}"] = 1
        logger.warning("Reddit sync stopped early (%s); it continues on the next run.", cut.reason)
        return
    logger.info(
        "Reddit: %d post(s) emitted, %d unchanged, %d deleted.",
        stats["emitted"],
        stats["unchanged"],
        stats["deleted"],
    )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def _clean_subreddits(subreddits: list[str] | None) -> tuple[str, ...]:
    names: dict[str, str] = {}
    for raw in subreddits or ():
        name = str(raw).strip()
        name = re.sub(r"^/?r/", "", name, flags=re.IGNORECASE).strip("/")
        if not _SUBREDDIT.fullmatch(name):
            raise ValueError(f"{raw!r} is not a subreddit name such as python or r/python")
        names.setdefault(name.lower(), name)
    if not names:
        raise ValueError("Select at least one subreddit with subreddits=")
    return tuple(names.values())


def _default_resource_name(subreddits: tuple[str, ...]) -> str:
    keys = sorted(name.lower() for name in subreddits)
    # Joining names with "_" can collide ("a_b" + "c", "a" + "b_c"); the hash cannot.
    scope = _digest(keys)[:8]
    return f"reddit_{'_'.join(keys)[:46].strip('_')}_{scope}"


def _positive(value: Any, what: str, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{what} must be a whole number of at least {minimum}")
    return value


def reddit_source(
    *,
    subreddits: list[str] | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    user_agent: str | None = None,
    username: str | None = None,
    password: str | None = None,
    include_comments: bool = True,
    max_comment_depth: int = DEFAULT_MAX_COMMENT_DEPTH,
    max_more_requests: int = DEFAULT_MAX_MORE_REQUESTS,
    refresh_days: float = DEFAULT_REFRESH_DAYS,
    first_sync_posts: int = DEFAULT_FIRST_SYNC_POSTS,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    resource_name: str | None = None,
    check_active: Callable[[], None] | None = None,
    service: Any = None,
    client: Any = None,
):
    """Return a ``dlt`` resource that yields one document per Reddit post.

    Hand the result to ``cognee.remember(...)`` with ``write_disposition="merge"``
    and ``primary_key="id"``.

    Args:
        subreddits: Subreddits to sync, by name (``python`` or ``r/python``). A
            subreddit that cannot be read on its first sync raises.
        client_id: The client id of a Reddit "script" app (under its name in
            https://www.reddit.com/prefs/apps).
        client_secret: The app's secret.
        user_agent: A descriptive User-Agent, as Reddit asks:
            ``<platform>:<app id>:<version> (by /u/<username>)``.
        username: With ``password``, the account the app belongs to: the password
            grant then replaces app-only OAuth, which some private subreddits need.
        password: The account's password.
        include_comments: Ingest each post's comment thread with it.
        max_comment_depth: Deepest reply level read (0 is a top-level comment).
        max_more_requests: ``/api/morechildren`` requests per thread, each loading up
            to 100 hidden comments. A thread cut short says so in its text.
        refresh_days: Posts newer than this are read again when their text, edit
            time or comment count changes. Older posts are only checked for deletion.
        first_sync_posts: Newest posts read from each subreddit on its first sync.
        max_requests: Request budget of one run, checked once a listing, a check or
            a thread is finished. A bigger sync finishes over several runs; a spent
            budget sets ``failed_budget`` but not ``failed``.
        resource_name: Staging table name, which scopes this sync's state and
            cleanup: lowercase letters, digits and underscores, up to 64. Defaults to
            one derived from the subreddits.
        check_active: Optional host authorization checkpoint, called before each
            request, during rate limit waits and around each row.
        service: Pre-built client (see :class:`RedditClient`). Mainly an injection
            point for tests and hosts. ``client`` is accepted as an alias.

    Returns:
        A ``dlt`` resource configured with ``primary_key="id"``,
        ``write_disposition="merge"``, a ``_deleted`` hard-delete column and the
        ``cognee_node_set`` column.
    """
    import dlt

    if isinstance(refresh_days, bool) or not isinstance(refresh_days, (int, float)):
        raise ValueError("refresh_days must be a number of days")
    if refresh_days < 0:
        raise ValueError("refresh_days must not be negative")
    config = _RedditConfig(
        subreddits=_clean_subreddits(subreddits),
        include_comments=bool(include_comments),
        max_comment_depth=_positive(max_comment_depth, "max_comment_depth", 0),
        max_more_requests=_positive(max_more_requests, "max_more_requests", 0),
        refresh_days=float(refresh_days),
        first_sync_posts=_positive(first_sync_posts, "first_sync_posts"),
        max_requests=_positive(max_requests, "max_requests"),
    )
    if resource_name is not None and not _RESOURCE_NAME.fullmatch(resource_name):
        raise ValueError(
            "resource_name must be lowercase letters, digits and underscores, up to 64 characters"
        )
    name = resource_name or _default_resource_name(config.subreddits)
    service = service or client
    if service is None:
        # Checked now, so a bad setting fails here rather than inside the pipeline.
        client_id = _clean(client_id, "client id")
        client_secret = _clean(client_secret, "client secret")
        user_agent = _clean(user_agent, "user agent")
        if bool(username) != bool(password):
            raise ValueError("Pass username= and password= together, or neither")
    stats: dict[str, int] = {}

    @dlt.resource(
        name=name,
        primary_key="id",
        write_disposition="merge",
        columns={
            "_deleted": {"data_type": "bool", "hard_delete": True},
            **dlt_utils.NODE_SET_COLUMN_HINT,
        },
    )
    def reddit_documents():
        stats.clear()
        state = dlt.current.resource_state()
        run_client = service or RedditClient(
            client_id,
            client_secret,
            user_agent,
            username=username,
            password=password,
            check_active=check_active,
        )
        try:
            rows = _iter_rows(run_client, config, state, stats, check_active)
            yield from dlt_utils.guarded_rows(rows, check_active)
        finally:
            if service is None:
                run_client.close()

    resource = reddit_documents()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "reddit")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, name)
    resource.cognee_sync_stats = stats
    return resource
