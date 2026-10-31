"""Mattermost connector for cognee: a dlt resource that syncs selected channels into memory.

Each thread (a root post and its replies) becomes one document, merged on the root
post id, and is meant to be handed to ``cognee.remember``::

    await cognee.remember(
        mattermost_source(
            "https://chat.example.com", token="<personal access token>", channels=[...]
        ),
        dataset_name="mattermost",
        primary_key="id",
        write_disposition="merge",  # required, the add pipeline defaults to "replace"
    )
"""

import copy
import hashlib
import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR

logger = get_logger("mattermost_connector")

MATTERMOST_SOURCE_NAME = "mattermost"
# Mattermost ids are 26 lowercase letters and digits; checked before one goes into a path.
_ID_RE = re.compile(r"^[a-z0-9]{26}$")
# dlt snake-cases table names and collapses repeated underscores, so only names it
# leaves unchanged are accepted: single underscores between letters and digits.
_RESOURCE_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_RESOURCE_NAME_MAX = 51
# http is only accepted for a server on this machine (the Docker preview).
_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")
# GET /channels/{id}/posts?since= returns at most this many posts and cannot be paged
# (verified on Mattermost 11.11.1), so a full answer may have left changes out.
_SINCE_CAP = 1000
# The largest per_page Mattermost accepts on the paged channel listing.
_PAGE_SIZE = 200
# POST /users/ids takes a list; 100 per request keeps the body small.
_USER_BATCH = 100
# Attempts per request: the first try plus four retries on 429, 5xx and dropped
# connections.
_MAX_RETRIES = 5
# cognee holds a process-wide staging lock while a dlt source runs, so a longer wait
# is not slept through: the run stops cleanly and the next one resumes.
_MAX_RETRY_AFTER = 30.0
# Runs in a row one channel may fail (lost access, a 5xx) while the others still sync.
# Raising in the same run would fail pipeline.run and dlt would keep no channel's
# progress, so a failure is counted, and the run raises once it keeps failing.
_FAILED_RUNS_BEFORE_RAISE = 3
# A full re-read starts over this many times when posts are deleted while it runs.
_LISTING_ATTEMPTS = 3
# The longest title, taken from the first line of the root post.
_TITLE_MAX = 80
_COLUMNS = {
    "_deleted": {"data_type": "bool", "hard_delete": True},
    "title": {"data_type": "text", "nullable": True},
    "content": {"data_type": "text", "nullable": True},
    "url": {"data_type": "text", "nullable": True},
    **dlt_utils.NODE_SET_COLUMN_HINT,
}


class MattermostAPIError(RuntimeError):
    """A Mattermost API call failed.

    ``status`` is the HTTP status when the server answered (``None`` otherwise).
    ``ends_run`` marks errors that would fail every channel the same way.
    """

    def __init__(self, message: str, status: int | None = None, ends_run: bool = False):
        super().__init__(message)
        self.status = status
        self.ends_run = ends_run


class MattermostAuthError(MattermostAPIError):
    """The server rejected the token (HTTP 401)."""

    def __init__(self, message: str):
        super().__init__(message, 401, ends_run=True)


class MattermostRateLimitError(MattermostAPIError):
    """Still rate limited after the retries, or asked to wait longer than we sleep."""

    def __init__(self, message: str):
        super().__init__(message, 429)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib keeps the Authorization header on a redirect to another host, so a
    # redirect is never followed: it surfaces as the 3xx HTTPError instead.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _endpoint(path: str) -> str:
    """The request path with ids replaced, for errors and logs."""
    return re.sub(r"/[a-z0-9]{26}(?=/|$)", "/{id}", path)


def _server_url(url: Any) -> str:
    """The server's base URL, https unless the server runs on this machine."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("Pass server_url=, like https://chat.example.com.")
    parsed = urllib.parse.urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if not host or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("server_url must be a plain URL like https://chat.example.com.")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and host in _LOOPBACK_HOSTS):
        raise ValueError(
            "server_url must use https (http is only accepted for a server on localhost), "
            "so the token never travels in clear text."
        )
    netloc = f"[{host}]" if ":" in host else host
    if parsed.port:
        netloc += f":{parsed.port}"
    return f"{parsed.scheme}://{netloc}{parsed.path.rstrip('/')}"


def _token(value: Any) -> str:
    """The token without surrounding whitespace, refused if it holds any inside.

    A pasted token often ends in a newline, and urllib quotes the whole header,
    token included, in the error it raises for one. The message never echoes it.
    """
    token = value.strip() if isinstance(value, str) else ""
    if not token or any(ch.isspace() or ord(ch) < 32 for ch in token):
        raise ValueError("Pass token=, a Mattermost personal access token (no spaces).")
    return token


def _retry_after(headers) -> float | None:
    # Mattermost's rate limiter sends both Retry-After and X-Ratelimit-Reset, in seconds.
    for name in ("Retry-After", "X-Ratelimit-Reset"):
        try:
            return max(0.0, float(headers.get(name)))
        except (TypeError, ValueError):
            continue
    return None


def _backoff(attempt: int) -> float:
    return float(min(2**attempt, 16))


# ---------------------------------------------------------------------------
# HTTP client (stdlib only)
# ---------------------------------------------------------------------------
class MattermostClient:
    """Minimal Mattermost REST client: ``request(method, path, params=, body=)``.

    Returns ``(payload, server_time_ms)``: the decoded JSON and the server's clock from
    the ``Date`` header (``None`` without one). Retries 429 (honouring the wait up to
    30 s), 5xx and dropped connections, and raises :class:`MattermostAPIError`
    otherwise. Requests only ever go to ``server_url``, and redirects are not followed.
    """

    def __init__(
        self,
        server_url: str,
        token: str,
        *,
        timeout: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        check_active: Callable[[], None] | None = None,
    ):
        self.server_url = _server_url(server_url)
        self._auth = f"Bearer {_token(token)}"
        self._timeout = timeout
        self._sleep = sleep
        self._check_active = check_active
        self._opener = urllib.request.build_opener(_NoRedirect)

    def __repr__(self) -> str:
        # The token is a password: keep it out of any repr, log or traceback.
        return f"MattermostClient(server_url={self.server_url!r}, token=<redacted>)"

    def _open(self, request):
        return self._opener.open(request, timeout=self._timeout)

    def _wait(self, seconds: float) -> None:
        # A host that revoked access must not wait through the retries.
        if self._check_active is not None:
            self._check_active()
        self._sleep(seconds)

    def request(
        self, method: str, path: str, *, params: dict | None = None, body: Any = None
    ) -> tuple[Any, int | None]:
        if not path.startswith("/"):
            raise ValueError("Mattermost paths start with '/'.")
        url = f"{self.server_url}/api/v4{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        endpoint = _endpoint(path)
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(_MAX_RETRIES):
            headers = {"Authorization": self._auth, "Accept": "application/json"}
            if data is not None:
                headers["Content-Type"] = "application/json"
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with self._open(request) as response:
                    raw = response.read()
                    date = response.headers.get("Date")
            except urllib.error.HTTPError as exc:
                # Every raise below uses ``from None``: the HTTPError carries the request
                # and its headers, and errors name the endpoint without ids.
                status = exc.code
                if status == 401:
                    raise MattermostAuthError(
                        "Mattermost rejected the token (HTTP 401). Check it was not revoked, "
                        "and that personal access tokens are enabled on the server."
                    ) from None
                if 300 <= status < 400:
                    raise MattermostAPIError(
                        f"Mattermost redirected {endpoint} (HTTP {status}); it is not followed, "
                        "so the token only ever goes to server_url. Check the URL.",
                        status,
                        ends_run=True,
                    ) from None
                if status == 429:
                    delay = _retry_after(exc.headers)
                    if delay is not None and delay > _MAX_RETRY_AFTER:
                        raise MattermostRateLimitError(
                            f"Mattermost asked to wait {delay:.0f}s on {endpoint}."
                        ) from None
                    if attempt == _MAX_RETRIES - 1:
                        raise MattermostRateLimitError(
                            f"Mattermost still rate limited {endpoint} after {_MAX_RETRIES} tries."
                        ) from None
                if (status == 429 or status >= 500) and attempt < _MAX_RETRIES - 1:
                    delay = _retry_after(exc.headers) if status == 429 else None
                    delay = delay if delay is not None else _backoff(attempt)
                    logger.warning(
                        "Mattermost: HTTP %s on %s, retrying in %.0fs (%d/%d).",
                        status,
                        endpoint,
                        delay,
                        attempt + 1,
                        _MAX_RETRIES,
                    )
                    self._wait(delay)
                    continue
                raise MattermostAPIError(
                    f"Mattermost returned HTTP {status} for {endpoint}.", status
                ) from None
            except (OSError, http.client.HTTPException) as exc:
                # URLError, timeouts, ConnectionResetError, RemoteDisconnected and
                # IncompleteRead: a dropped connection is retried like a 5xx.
                if attempt < _MAX_RETRIES - 1:
                    self._wait(_backoff(attempt))
                    continue
                # ``from None``: the error's text can carry the request URL.
                raise MattermostAPIError(
                    f"Mattermost could not be reached ({type(exc).__name__}) for {endpoint}.",
                    ends_run=True,
                ) from None
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = None
            if not isinstance(payload, dict | list):
                # A login or proxy page answered with 200: it would read the same for
                # every channel, so the run stops instead of counting failures.
                raise MattermostAPIError(
                    f"Mattermost returned HTTP 200 without JSON for {endpoint}.",
                    200,
                    ends_run=True,
                )
            return payload, _date_ms(date)


def _date_ms(value: str | None) -> int | None:
    try:
        return int(parsedate_to_datetime(value).timestamp() * 1000) if value else None
    except (TypeError, ValueError):
        return None


class _CheckedClient:
    """Calls the host's ``check_active`` before every request of an injected client."""

    def __init__(self, client: Any, check_active: Callable[[], None]):
        self._client = client
        self._check_active = check_active

    def request(self, method: str, path: str, **kwargs: Any) -> tuple[Any, int | None]:
        self._check_active()
        return self._client.request(method, path, **kwargs)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _is_content(post: Any) -> bool:
    """A live message: not deleted and not a system event.

    Every edit also leaves a hidden copy of the old version (``original_id`` set),
    which comes back already deleted and under its own id, so it is never read as
    content and never forgets the edited post.
    """
    return (
        isinstance(post, dict)
        and isinstance(post.get("id"), str)
        and not post.get("delete_at")
        # joins, leaves, header changes and other events have a "system_" type
        and not str(post.get("type") or "").startswith("system_")
    )


def _root_of(post: dict) -> str:
    return post.get("root_id") or post["id"]


def _when(ms: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
    except (TypeError, ValueError, OverflowError, OSError):
        return "unknown time"


def _user_label(users: dict, user_id: Any) -> str:
    """A person's name and username, never their email address."""
    user = users.get(user_id) or {}
    full = " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p).strip()
    username = user.get("username")
    if full and username:
        return f"{full} (@{username})"
    if username:
        return f"@{username}"
    return full or f"user {user_id}"


def _message(post: dict) -> str:
    text = str(post.get("message") or "").strip()
    if text:
        return text
    return "(attachment)" if post.get("file_ids") else "(no text)"


def thread_to_row(
    root: dict, replies: list[dict], users: dict, channel: dict, server_url: str
) -> dict[str, Any]:
    """Render a thread as one deterministic document row.

    Replies are ordered by creation time and id, so the same thread always renders
    the same text whatever order the server sent it in. Timestamps of edits are left
    out: the edited text is what matters, and it already changes the content.
    """
    channel_label = channel.get("display_name") or channel.get("name") or "channel"
    first_line = _message(root).splitlines()[0]
    title = first_line if len(first_line) <= _TITLE_MAX else first_line[: _TITLE_MAX - 3] + "..."
    lines = [f"Channel: {channel_label}", ""]
    for post in [root, *sorted(replies, key=lambda p: (p.get("create_at") or 0, p["id"]))]:
        who = _user_label(users, post.get("user_id"))
        lines.append(f"--- {who}, {_when(post.get('create_at'))}")
        lines.append(_message(post))
        lines.append("")
    row = {
        "id": root["id"],
        "title": title,
        "content": "\n".join(lines).strip(),
        "url": f"{server_url}/_redirect/pl/{root['id']}",
        "_deleted": False,
    }
    if channel.get("node_set"):
        row[dlt_utils.NODE_SET_COLUMN] = [channel["node_set"]]
    return row


def deleted_row(root_id: str) -> dict[str, Any]:
    return {"id": root_id, "_deleted": True}


def _fingerprint(row: dict) -> str:
    return hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
def _posts(payload: Any, endpoint: str) -> dict[str, dict]:
    """The ``posts`` map of a post list, refusing an answer of another shape."""
    posts = payload.get("posts") if isinstance(payload, dict) else None
    if posts is None and isinstance(payload, dict) and payload.get("order") == []:
        return {}  # an empty post list can come with "posts": null
    if not isinstance(posts, dict):
        raise MattermostAPIError(f"Mattermost sent no post list for {endpoint}.")
    return {pid: p for pid, p in posts.items() if isinstance(p, dict) and p.get("id") == pid}


class _Run:
    """One sync run over the selected channels, sharing lookups between them."""

    def __init__(self, client: Any, server_url: str, stats: dict[str, int]):
        self.client = client
        self.server_url = server_url
        self.stats = stats
        self.users: dict[str, dict] = {}
        self.teams: dict[str, str] = {}

    def get(self, path: str, **params: Any) -> tuple[Any, int | None]:
        return self.client.request("GET", path, params=params or None)

    # -- lookups -----------------------------------------------------------
    def channel(self, channel_id: str) -> dict:
        channel, _ = self.get(f"/channels/{channel_id}")
        if not isinstance(channel, dict) or channel.get("id") != channel_id:
            raise MattermostAPIError("Mattermost sent no channel for /channels/{id}.")
        return channel

    def node_set(self, channel: dict) -> str:
        """``<team>/<channel name>``, so recall can be scoped to one channel."""
        team_id = channel.get("team_id")
        if team_id and team_id not in self.teams:
            try:
                team, _ = self.get(f"/teams/{team_id}")
                self.teams[team_id] = team.get("name") if isinstance(team, dict) else ""
            except MattermostAPIError as exc:
                if exc.ends_run or isinstance(exc, MattermostRateLimitError):
                    raise
                self.teams[team_id] = ""  # only used to name the node set
        team = self.teams.get(team_id or "", "")
        # direct and group messages have no team and an id-like name
        return f"{team}/{channel.get('name') or channel['id']}" if team else "direct_messages"

    def load_users(self, user_ids: set[str]) -> None:
        """Names for the given users. A failed lookup only costs the names."""
        missing = sorted(uid for uid in user_ids if uid and uid not in self.users)
        try:
            for i in range(0, len(missing), _USER_BATCH):
                found, _ = self.client.request(
                    "POST", "/users/ids", body=missing[i : i + _USER_BATCH]
                )
                for user in found if isinstance(found, list) else []:
                    if isinstance(user, dict) and isinstance(user.get("id"), str):
                        self.users[user["id"]] = user
        except MattermostAPIError as exc:
            if exc.ends_run or isinstance(exc, MattermostRateLimitError):
                raise
            logger.warning("Mattermost: user lookup failed (%s); names fall back to ids.", exc)

    # -- reading -----------------------------------------------------------
    def since(self, channel_id: str, cursor: int) -> dict[str, dict]:
        # ``since`` is exclusive, and one change can write several posts in the same
        # millisecond, so the window starts 1 ms early; the edge set skips repeats.
        payload, _ = self.get(f"/channels/{channel_id}/posts", since=cursor - 1)
        return _posts(payload, "/channels/{id}/posts")

    def listing(self, channel_id: str) -> tuple[dict[str, dict], int]:
        """Every live post of a channel, and the server time before the first page.

        The listing pages by offset, newest first. Deleting a post that was already
        read shifts the later pages by one, so one live post would be skipped; new
        posts only cause repeats. So the posts changed since the read began are asked
        for afterwards, and the read starts over when one it returned was deleted.
        """
        for attempt in range(_LISTING_ATTEMPTS):
            posts, started = self._read_pages(channel_id)
            during = self.since(channel_id, started)
            shifted = any(p.get("delete_at") and pid in posts for pid, p in during.items())
            if not shifted and len(during) < _SINCE_CAP:
                break
            if not shifted and attempt == _LISTING_ATTEMPTS - 1:
                # Over 1000 changes during every read: none of those returned shifted a
                # page, and a busy channel must not fail for good, so the read is kept.
                logger.warning(
                    "Mattermost: a channel changed more than %d times while it was read.",
                    _SINCE_CAP,
                )
                break
        else:
            raise MattermostAPIError(
                "Mattermost posts kept being deleted while the channel was read; retried next run."
            )
        # Date has second precision: the next window starts a second early.
        return posts, started - 1000

    def _read_pages(self, channel_id: str) -> tuple[dict[str, dict], int]:
        posts: dict[str, dict] = {}
        started: int | None = None
        first_ids: set[str] = set()
        page = 0
        while True:
            payload, server_ms = self.get(
                f"/channels/{channel_id}/posts", page=page, per_page=_PAGE_SIZE
            )
            if started is None:
                started = server_ms
            batch = _posts(payload, "/channels/{id}/posts")
            order = payload.get("order") if isinstance(payload, dict) else None
            ids = tuple(order) if isinstance(order, list) else tuple(batch)
            if ids and ids[0] in first_ids:
                raise MattermostAPIError("Mattermost post pages did not move forward; stopping.")
            if ids:
                first_ids.add(ids[0])
            posts.update(batch)
            if len(ids) < _PAGE_SIZE:
                break
            page += 1
        if started is None:
            raise MattermostAPIError(
                "Mattermost sent no Date header, so no cursor can be kept.", ends_run=True
            )
        return posts, started

    def thread(self, root_id: str) -> dict[str, dict] | None:
        """A thread's posts, or ``None`` when its root was deleted."""
        try:
            payload, _ = self.get(f"/posts/{root_id}/thread")
        except MattermostAPIError as exc:
            if exc.status == 404:
                return None
            raise
        posts = _posts(payload, "/posts/{id}/thread")
        root = posts.get(root_id)
        if root is None or root.get("delete_at"):
            return None
        return posts


def _render_threads(run: _Run, channel: dict, threads: dict[str, list[dict]]) -> dict[str, dict]:
    """Rows for threads given as {root id: [root, *replies]} of live posts."""
    run.load_users({p.get("user_id") for posts in threads.values() for p in posts})
    rows = {}
    for root_id, posts in threads.items():
        root = next(p for p in posts if p["id"] == root_id)
        replies = [p for p in posts if p["id"] != root_id]
        rows[root_id] = thread_to_row(root, replies, run.users, channel, run.server_url)
    return rows


def _group(posts: dict[str, dict]) -> tuple[dict[str, list[dict]], set[str]]:
    """Live posts grouped by thread, and the roots of replies whose root is not among them."""
    threads: dict[str, list[dict]] = {}
    orphans: set[str] = set()
    live = {pid: p for pid, p in posts.items() if _is_content(p)}
    for post in live.values():
        root_id = _root_of(post)
        if root_id in live and not live[root_id].get("root_id"):
            threads.setdefault(root_id, []).append(post)
        elif post.get("root_id"):
            orphans.add(root_id)
    return threads, orphans


def _plan_channel(
    run: _Run, channel_id: str, state: dict, forget_archived: bool
) -> tuple[list[dict], dict | None]:
    """The rows for one channel and the state to keep once they are out (``None``
    drops it)."""
    channel = run.channel(channel_id)
    known: dict[str, str] = dict(state.get("threads", {}))
    if channel.get("delete_at"):
        run.stats["archived"] += 1
        if forget_archived:
            logger.info("Mattermost: a selected channel is archived; forgetting its threads.")
            return [deleted_row(root) for root in sorted(known)], None
        logger.info("Mattermost: a selected channel is archived; keeping it, not syncing it.")
        return [], {k: v for k, v in state.items() if k != "failed_runs"}

    channel["node_set"] = run.node_set(channel)
    full = "cursor" not in state
    rendered: dict[str, dict] = {}
    gone: set[str] = set()
    if not full:
        cursor = int(state["cursor"])
        edge = set(state.get("edge", []))
        changed = run.since(channel_id, cursor)
        if len(changed) >= _SINCE_CAP:
            logger.warning(
                "Mattermost: %d changes since the last sync (the most one request returns); "
                "re-reading the whole channel instead.",
                len(changed),
            )
            full = True
        else:
            fresh = {
                pid: p for pid, p in changed.items() if f"{pid}:{p.get('update_at')}" not in edge
            }
            roots: set[str] = set()
            for post in fresh.values():
                if str(post.get("type") or "").startswith("system_"):
                    continue
                if not post.get("root_id") and post.get("delete_at"):
                    gone.add(post["id"])  # a deleted root takes its thread with it
                else:
                    roots.add(_root_of(post))
            threads = {}
            for root_id in sorted(roots - gone):
                thread = run.thread(root_id)
                if thread is None:
                    gone.add(root_id)
                    continue
                grouped, _ = _group(thread)
                if root_id in grouped:
                    threads[root_id] = grouped[root_id]
            rendered = _render_threads(run, channel, threads)
            stamps = [int(p.get("update_at") or 0) for p in changed.values()]
            new_cursor = max([cursor, *stamps])
            new_edge = {
                f"{pid}:{p.get('update_at')}"
                for pid, p in changed.items()
                if int(p.get("update_at") or 0) == new_cursor
            }
            if new_cursor == cursor:
                new_edge |= edge
    if full:
        listed, new_cursor = run.listing(channel_id)
        threads, orphans = _group(listed)
        new_edge = set()
        # A stored root missing from the listing, or the root of a reply that came
        # without it, is read on its own: only a root that is really gone is forgotten.
        for root_id in sorted((set(known) | orphans) - set(threads)):
            thread = run.thread(root_id)
            if thread is None:
                gone.add(root_id)
                continue
            grouped, _ = _group(thread)
            if root_id in grouped:
                threads[root_id] = grouped[root_id]
            else:
                gone.add(root_id)
        rendered = _render_threads(run, channel, threads)

    rows: list[dict] = []
    for root_id in sorted(rendered):
        fingerprint = _fingerprint(rendered[root_id])
        if known.get(root_id) == fingerprint:
            run.stats["skipped"] += 1
            continue
        known[root_id] = fingerprint
        rows.append(rendered[root_id])
    for root_id in sorted(gone & set(known)):
        known.pop(root_id)
        rows.append(deleted_row(root_id))
    new_state = {"cursor": new_cursor, "edge": sorted(new_edge), "threads": known}
    return rows, new_state


def _check_owner(state: dict, server_url: str) -> None:
    """Refuse a sync state written for another Mattermost server."""
    if state.get("server") not in (None, server_url):
        raise ValueError(
            f"This sync state belongs to {state['server']}, not {server_url}. "
            "Give each server its own resource_name."
        )


def sync_channels(
    client: Any,
    state: dict,
    server_url: str,
    channels: list[str],
    stats: dict[str, int],
    *,
    forget_archived: bool = False,
) -> Iterator[dict[str, Any]]:
    """Yield changed threads and tombstones for the selected channels.

    A channel that fails (lost access, a 5xx after the retries) keeps its state and
    is retried next run while the others still sync; it raises once it has failed
    ``_FAILED_RUNS_BEFORE_RAISE`` runs in a row, or when every channel failed. A
    rejected token, an unreachable server or a 200 without JSON raise at once, and a
    rate limit stops the rest of the run cleanly. Channels dropped from the selection
    are forgotten. The state is written after the last row of each channel, and dlt
    keeps it only once the load succeeded.
    """
    for key in ("scanned", "deleted", "skipped", "archived", "failed"):
        stats.setdefault(key, 0)
    _check_owner(state, server_url)
    saved: dict[str, dict] = state.get("channels", {})
    channel_states = copy.deepcopy(saved)
    selected = set(channels)

    pending: list[tuple[list[dict], str, dict | None]] = []
    for channel_id in sorted(set(channel_states) - selected):
        roots = sorted(channel_states[channel_id].get("threads", {}))
        logger.info("Mattermost: a channel left the selection; forgetting %d threads.", len(roots))
        pending.append(([deleted_row(root) for root in roots], channel_id, None))

    run = _Run(client, server_url, stats)
    failed: list[str] = []
    stopped = None
    for channel_id in channels:
        before = channel_states.get(channel_id, {})
        try:
            rows, after = _plan_channel(run, channel_id, before, forget_archived)
        except MattermostRateLimitError:
            stopped = "rate_limit"
            break
        except MattermostAPIError as exc:
            if exc.ends_run:
                raise
            failed.append(channel_id)
            runs = int(before.get("failed_runs", 0)) + 1
            if runs >= _FAILED_RUNS_BEFORE_RAISE:
                raise MattermostAPIError(
                    f"A Mattermost channel failed {runs} runs in a row: {exc}", exc.status
                ) from exc
            logger.warning(
                "Mattermost: a channel failed (%s); the others still sync and it is retried "
                "next run (%d of %d failed runs before this raises).",
                exc,
                runs,
                _FAILED_RUNS_BEFORE_RAISE,
            )
            pending.append(([], channel_id, {**before, "failed_runs": runs}))
            continue
        pending.append((rows, channel_id, after))

    if failed and len(failed) == len(channels):
        raise MattermostAPIError("Every selected Mattermost channel failed this run.")
    stats["failed"] = len(failed) + (1 if stopped else 0)
    if failed:
        stats["failed_channels"] = len(failed)
    if stopped:
        stats[f"failed_{stopped}"] = 1
        logger.warning("Mattermost: rate limited; stopping this run, the next sync resumes.")

    for rows, channel_id, after in pending:
        for row in rows:
            stats["deleted" if row["_deleted"] else "scanned"] += 1
            yield row
        # Only reached once this channel's rows are out.
        if after is None:
            channel_states.pop(channel_id, None)
        else:
            channel_states[channel_id] = after
    state["server"] = server_url
    state["channels"] = channel_states


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def _default_resource_name(server_url: str) -> str:
    parsed = urllib.parse.urlparse(server_url)
    slug = re.sub(r"[^a-z0-9]+", "_", f"{parsed.hostname}_{parsed.port or ''}{parsed.path}")
    slug = slug.strip("_")
    name = f"mattermost_{slug}"
    if len(name) > _RESOURCE_NAME_MAX:
        digest = hashlib.sha256(server_url.encode()).hexdigest()[:8]
        name = f"{name[: _RESOURCE_NAME_MAX - 9].rstrip('_')}_{digest}"
    return name


def mattermost_source(
    server_url: str,
    *,
    token: str | None = None,
    channels: list[str] | tuple[str, ...],
    forget_archived: bool = False,
    resource_name: str | None = None,
    check_active: Callable[[], None] | None = None,
    client: Any = None,
    service: Any = None,
):
    """Return a ``dlt`` resource that yields the selected channels' threads for ``remember``.

    Args:
        server_url: The server's address, like ``https://chat.example.com`` (http only
            for a server on localhost).
        token: A personal access token (or a bot token) of an account that can read the
            channels. Tokens are disabled by default; see the README.
        channels: The ids of the channels to sync. Dropping one later forgets its threads.
        forget_archived: Forget the threads of a selected channel once it is archived.
            By default they stay in memory and the channel stops syncing.
        resource_name: Table name and key of the sync state: lowercase letters and
            digits with single underscores, at most 51 characters. Defaults to
            ``mattermost_<host>``. Keep it fixed across runs.
        check_active: Optional host authorization checkpoint, called before every API
            request, before every retry wait and around every row; raising stops the sync.
        client: Object with ``request(method, path, params=, body=)`` returning
            ``(payload, server_time_ms)``; ``service`` is an alias.

    Returns:
        A ``dlt`` resource with ``primary_key="id"``, ``write_disposition="merge"`` and
        an ``_deleted`` hard-delete column. After a run, ``resource.cognee_sync_stats``
        holds counts only: ``scanned``, ``deleted``, ``skipped``, ``archived``,
        ``failed``, plus ``failed_channels`` or ``failed_rate_limit``.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 2:
        raise RuntimeError(
            "The Mattermost connector requires cognee 1.6.3 or later (table-scoped "
            "document cleanup and per-row node sets). Upgrade cognee before syncing."
        )
    if client is not None and service is not None and client is not service:
        raise ValueError("Pass client= or service=, not both.")
    client = client if client is not None else service
    server_url = _server_url(server_url)
    if isinstance(channels, str) or not channels:
        raise ValueError("Pass channels=, a non-empty list of Mattermost channel ids.")
    channels = list(dict.fromkeys(channels))
    bad = [c for c in channels if not isinstance(c, str) or not _ID_RE.match(c)]
    if bad:
        raise ValueError(
            f"{len(bad)} channel id(s) are not 26 lowercase letters and digits; use the ID "
            "from the channel's Info panel, not its name."
        )
    if resource_name is not None and (
        not isinstance(resource_name, str)
        or len(resource_name) > _RESOURCE_NAME_MAX
        or not _RESOURCE_NAME_RE.match(resource_name)
    ):
        raise ValueError(
            "resource_name must be lowercase letters and digits with single underscores, "
            "starting with a letter and at most 51 characters, so dlt uses it unchanged as "
            "the table name."
        )
    resource_name = resource_name or _default_resource_name(server_url)
    if client is None:
        client = MattermostClient(server_url, token, check_active=check_active)
    if check_active is not None:
        client = _CheckedClient(client, check_active)
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns=_COLUMNS,
    )
    def mattermost_threads():
        stats.clear()
        rows = sync_channels(
            client,
            dlt.current.resource_state(),
            server_url,
            channels,
            stats,
            forget_archived=forget_archived,
        )
        yield from dlt_utils.guarded_rows(rows, check_active)

    resource = mattermost_threads()
    # Threads are prose: route them through cognify (document mode).
    setattr(resource, DOCUMENT_SOURCE_ATTR, MATTERMOST_SOURCE_NAME)
    # Keep the cursors per resource_name (and, in cognee, per dataset).
    setattr(resource, PIPELINE_SCOPE_ATTR, resource_name)
    resource.cognee_sync_stats = stats
    return resource
