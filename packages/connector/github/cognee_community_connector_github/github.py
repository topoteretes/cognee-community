"""GitHub connector for cognee: a ``dlt`` source that turns GitHub repositories into memory.

One document per repository, issue, pull request and release (and, opt-in,
default-branch commit), synced incrementally with forget-on-delete. Hand the
source to :func:`cognee.remember`::

    await cognee.remember(
        github_source(repos=["owner/name"]),  # GITHUB_TOKEN from env
        dataset_name="github",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

The README describes what is stored, how changes and deletions are found, and
what a run does when GitHub fails part of the way through.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlencode

import dlt
import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from dlt.common.normalizers.naming.snake_case import NamingConvention

logger = get_logger("github_connector")

# Default dlt resource / staging-table name, and the document-source tag for its rows.
GITHUB_TABLE_NAME = "github_documents"
GITHUB_SOURCE_NAME = "github"

_API_BASE = "https://api.github.com"
_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "cognee-community-connector-github",
}
_RAW = "application/vnd.github.raw"
# Per request: a generous bound for one API call, so a stalled connection fails and is retried.
_TIMEOUT_SECONDS = 60
_PER_PAGE = 100  # GitHub's maximum page size for every listing used here

# Five attempts per request, with 1, 2, 4, 8 s between them (about 15 s of
# outage tolerated per request), or GitHub's own wait on a rate limit.
_MAX_RETRIES = 5
# The 5xx answers GitHub's docs describe as temporary.
_TRANSIENT_STATUSES = (500, 502, 503, 504)
# The longest single wait, and the most a run waits in all. cognee holds its
# dlt staging lock during a sync, so a longer rate-limit wait (GitHub's primary
# window resets hourly), or a wait past the budget, stops the run cleanly
# instead; the next run carries on.
_MAX_WAIT_SECONDS = 60
_MAX_TOTAL_WAIT_SECONDS = 120
# GitHub's advice for a secondary limit that says nothing about how long to wait.
_SECONDARY_LIMIT_WAIT_SECONDS = 60.0

# GitHub applies updated_at with a lag of several seconds (seen live: no move
# 3 s after a review, moved 15 s later), so the change listing starts well
# before the cursor and a fetched copy is only trusted as final once it is a
# minute older than its timestamp. Both are wide margins over the lag seen.
_CURSOR_OVERLAP = timedelta(minutes=5)
_SETTLE_MARGIN = timedelta(seconds=60)

# Fresh issue failures in a row (5xx or no answer, after retries) that mean
# GitHub itself is down: the run stops cleanly. One or two can be a single
# broken item; three different items failing back to back are rarely a
# coincidence, and a stop costs nothing, as the next run resumes. Issues already
# kept for a retry never count.
_OUTAGE_STREAK = 3
# Rules for forgetting (deleting the document of) an issue or pull request that
# cannot be read: listed but answering 403/404 at the same updated_at, or
# missing from the listing with no clear answer. Only finished runs count, and
# for a 403/404 only runs that showed access to the kind working in that
# repository (see _Run.settle).
# - 3 runs: three separate observations, so one glitch or a short incident on
#   GitHub's side cannot do it.
# - 24 hours: outlasts a working day of permission changes and a maintenance
#   window. On a cron of minutes this is the floor that does the work.
# - On a weekly cron the 3 runs take 2 to 3 weeks; accepted, because forgetting
#   too early deletes a document that is still valid, while forgetting late only
#   keeps a stale one a little longer.
_GIVE_UP_RUNS = 3
_GIVE_UP_AFTER = timedelta(hours=24)
# A forgotten listed issue is asked again when its updated_at moves, or a day
# after it was last asked: one request per forgotten issue per day, so one that
# becomes readable again is back within a day without retrying hidden issues on
# every run.
_REASK_AFTER = timedelta(hours=24)
# Unchanged items read, newest first, to check access to a kind in a repository
# (see _Run._probe): one of them may be hidden from the token itself.
_PROBE_TRIES = 3
# GitHub's Date header and the local clock differing by more than this is
# logged, and no item is forgotten in that run (see _run_clocks).
_CLOCK_TOLERANCE = timedelta(seconds=60)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# A pull request lists at most this many changed files: one page of /files at
# GitHub's maximum page size, so a huge pull request costs one request.
_MAX_FILES = 100

_STAT_KEYS = ("scanned", "skipped", "failed", "deleted", "kept", "given_up")
# Row-id prefix -> what a warning calls that kind of item.
_KIND_NAMES = {"issue": "issues", "pr": "pull requests"}
_KIND_NAME = {"issue": "issue", "pr": "pull request"}


class GitHubAPIError(RuntimeError):
    """An error answer from GitHub (``status``), or no answer at all (``status`` 0).

    The message names only the status and the endpoint. GitHub's own message is
    on ``detail``, with the token cut out; a response body never is. ``rate_limited`` is
    True when GitHub was still rate limiting (or asked for too long a wait).
    """

    def __init__(self, status: int, endpoint: str, detail: str = "", rate_limited: bool = False):
        answer = f"HTTP {status}" if status else "no answer"
        limited = " (rate limited)" if rate_limited else ""
        super().__init__(f"GitHub {endpoint}: {answer}{limited}")
        self.status = status
        self.endpoint = endpoint
        self.detail = detail
        self.rate_limited = rate_limited


class GitHubResponseError(RuntimeError):
    """GitHub answered with a body of the wrong shape, or a listing that never ends.

    Never read as an empty listing or a skippable issue: it aborts the run.
    """


class _CutShortError(Exception):
    """Internal: stop the run cleanly, keeping what was rendered so far."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _make_session(token: str) -> requests.Session:
    """A ``requests`` session that sends the token as ``Authorization: Bearer``."""
    session = requests.Session()
    session.headers.update({**_HEADERS, "Authorization": f"Bearer {token}"})
    return session


def _check_token(token: str) -> None:
    """Refuse a token ``requests`` would reject, without ever echoing it.

    A pasted token often ends in a newline; requests then raises an error whose
    message holds the whole header, token included.
    """
    if any(char.isspace() or not char.isprintable() for char in token):
        raise ValueError("The GitHub token contains whitespace or control characters")


class _Client:
    """One run's access to GitHub: the session, the host's check, the waits, the clock."""

    def __init__(
        self, session: Any, check_active: Callable[[], None] | None = None, secret: str = ""
    ):
        self.session = session
        self.check_active = check_active
        # The token, only to cut it out of GitHub's error messages.
        headers = getattr(session, "headers", None)
        bearer = (headers.get("Authorization", "") if headers else "").removeprefix("Bearer ")
        self.secret = secret or bearer
        # GitHub's clock at its first answer (the Date header), see _run_clocks.
        self.server_now: datetime | None = None
        self.waited = 0.0  # seconds slept this run
        self.out_of_time = False  # a wait was refused for the run's budget

    def check(self) -> None:
        if self.check_active is not None:
            self.check_active()

    def can_wait(self, seconds: float) -> bool:
        """Whether a wait fits in the run's budget (the single-wait cap is the caller's)."""
        if self.waited + seconds > _MAX_TOTAL_WAIT_SECONDS:
            self.out_of_time = True
            return False
        return True

    def wait(self, seconds: float) -> None:
        self.check()
        self.waited += seconds
        time.sleep(seconds)


def _as_client(client: Any) -> _Client:
    return client if isinstance(client, _Client) else _Client(client)


def _url(path: str, params: dict | None = None) -> str:
    """The absolute URL for an API path. ``Link`` URLs are already absolute."""
    url = path if path.startswith("https://") else f"{_API_BASE}{path}"
    if not url.startswith(_API_BASE + "/"):
        # The session carries the token: never send it anywhere else.
        raise ValueError(f"refusing to call a non-GitHub-API URL: {url}")
    return f"{url}?{urlencode(params)}" if params else url


def _endpoint(url: str) -> str:
    """The path of an API URL without its query, for messages and logs."""
    return url.removeprefix(_API_BASE).split("?")[0]


def _get(
    client: Any,
    url: str,
    *,
    etag: str | None = None,
    accept: str | None = None,
    follow_redirects: bool = True,
    ok: tuple[int, ...] = (200,),
) -> Any:
    """GET an absolute API URL and return the response, retrying rate limits and outages.

    A ``304`` (only possible when ``etag`` is sent) and any status in ``ok``
    are returned. A rate limit is waited out up to ``_MAX_WAIT_SECONDS``; a
    longer one raises at once. 5xx and network errors back off, and anything
    else raises :class:`GitHubAPIError`, including a ``403`` that is not a
    rate limit. No wait is taken past the run's ``_MAX_TOTAL_WAIT_SECONDS``.
    """
    client = _as_client(client)
    endpoint = _endpoint(url)
    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    if accept:
        headers["Accept"] = accept
    attempt = 0
    while True:
        last_attempt = attempt == _MAX_RETRIES - 1
        client.check()
        try:
            response = client.session.get(
                url, headers=headers, timeout=_TIMEOUT_SECONDS, allow_redirects=follow_redirects
            )
        except requests.RequestException as exc:
            # Only the type: the message of some requests errors holds a header.
            delay = float(2**attempt)
            if last_attempt or not client.can_wait(delay):
                raise GitHubAPIError(0, endpoint, type(exc).__name__) from None
            logger.warning(
                "GitHub: %s on %s; retrying in %.0fs.", type(exc).__name__, endpoint, delay
            )
            client.wait(delay)
            attempt += 1
            continue

        status = response.status_code
        if status in ok or (etag and status == 304):
            if client.server_now is None:
                client.server_now = _server_time(response)
            return response
        wait = _rate_limit_wait(response)
        if wait is not None:
            if wait > _MAX_WAIT_SECONDS or last_attempt or not client.can_wait(wait):
                raise GitHubAPIError(status, endpoint, f"wait {wait:.0f}s", rate_limited=True)
            logger.warning("GitHub: rate limited on %s; waiting %.0fs.", endpoint, wait)
            client.wait(wait)
            attempt += 1
            continue
        if status in _TRANSIENT_STATUSES and not last_attempt and client.can_wait(2**attempt):
            delay = float(2**attempt)
            logger.warning("GitHub: HTTP %d on %s; retrying in %.0fs.", status, endpoint, delay)
            client.wait(delay)
            attempt += 1
            continue
        detail = _redact(_error_detail(response), client.secret)[:200]
        raise GitHubAPIError(status, endpoint, detail)


def _rate_limit_wait(response: Any) -> float | None:
    """Seconds to wait if ``response`` is a rate limit, else ``None``.

    GitHub signals a rate limit with ``429``, or with ``403`` plus either
    ``retry-after`` (secondary limits) or ``x-ratelimit-remaining: 0`` (the
    primary limit). A secondary limit can also come as a bare ``403`` whose
    message says "secondary rate limit"; GitHub's advice is then to wait a
    minute. Any other ``403`` is a permission error. Without ``retry-after`` the
    wait runs until ``x-ratelimit-reset``, measured against GitHub's own
    ``Date`` so a wrong local clock cannot shorten it.
    """
    headers = response.headers or {}
    retry_after = headers.get("Retry-After")
    exhausted = headers.get("X-RateLimit-Remaining") == "0"
    secondary = "secondary rate limit" in _error_detail(response).lower()
    if response.status_code != 429 and not (
        response.status_code == 403 and (retry_after or exhausted or secondary)
    ):
        return None
    try:
        return float(retry_after)
    except (TypeError, ValueError):
        pass
    try:
        reset = float(headers.get("X-RateLimit-Reset"))
    except (TypeError, ValueError):
        return _SECONDARY_LIMIT_WAIT_SECONDS
    now = _server_time(response) or datetime.now(UTC)
    return max(reset - now.timestamp(), 0.0) + 1.0


def _error_detail(response: Any) -> str:
    """GitHub's ``message`` in full, or nothing. Never the raw body.

    A caller that keeps it cuts it to length only after ``_redact``, so a token
    that would straddle the cut is still found whole.
    """
    try:
        message = response.json()["message"]
    except (ValueError, KeyError, TypeError):
        return ""
    return message if isinstance(message, str) else ""


def _redact(text: str, secret: str) -> str:
    """``text`` with the token cut out, in any case, in case GitHub echoes it back.

    A token too short to replace safely (it could match ordinary words) drops
    the whole text when it occurs in it. Only the whole token is matched: a part
    of one cannot be told from ordinary text.
    """
    if not secret:
        return text
    pattern = re.compile(re.escape(secret), re.IGNORECASE)
    if not pattern.search(text):
        return text
    return pattern.sub("[token]", text) if len(secret) >= 8 else ""


def _json(response: Any, shape: type) -> Any:
    """The JSON body of a ``200``, which must be a ``list`` or a ``dict`` as expected.

    Anything else (not JSON, or the wrong shape) raises: an odd body must never
    be read as an empty listing, which would look like deletions.
    """
    status = response.status_code
    try:
        body = response.json()
    except ValueError:
        raise GitHubResponseError(f"GitHub answered {status} without JSON") from None
    if not isinstance(body, shape):
        raise GitHubResponseError(
            f"GitHub answered {status} with a {type(body).__name__}, not a {shape.__name__}"
        )
    return body


def _next_url(response: Any) -> str | None:
    """The ``rel="next"`` URL of a ``Link`` header, used exactly as given."""
    for part in ((response.headers or {}).get("Link") or "").split(","):
        url, _, rel = part.partition(";")
        if rel.strip() == 'rel="next"':
            return url.strip().strip("<>")
    return None


def _server_time(response: Any) -> datetime | None:
    """GitHub's clock when it answered, from the ``Date`` response header.

    ``None`` when the header is missing or unreadable. A zone of ``-0000``
    (unknown) is read as UTC, the zone HTTP dates are in.
    """
    try:
        moment = parsedate_to_datetime((response.headers or {}).get("Date"))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _parse(stamp: str) -> datetime:
    """Parse a GitHub timestamp such as ``2026-10-14T20:06:28Z``."""
    return datetime.fromisoformat(stamp)


def _stamp(moment: datetime) -> str:
    """Format a moment the way GitHub does (UTC, whole seconds, ``Z``)."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_settled(updated_at: str, fetched_at: datetime | None) -> bool:
    """True when a copy fetched at ``fetched_at`` is final for its ``updated_at``.

    GitHub timestamps have one-second resolution and are applied with a lag,
    so a copy fetched shortly after its timestamp may still miss a change that
    carries the same one. Once GitHub's own clock is ``_SETTLE_MARGIN`` past
    it, that cannot happen any more. Without a usable ``Date`` the answer is no,
    and the item is simply fetched again on the next run.
    """
    if fetched_at is None or not updated_at:
        return False
    return _parse(updated_at) <= fetched_at - _SETTLE_MARGIN


# ---------------------------------------------------------------------------
# Listings with ETags
# ---------------------------------------------------------------------------
def _list_pages(
    client: Any, first_url: str, previous: dict, key: Callable[[dict], list]
) -> tuple[list[dict], list[list], dict, datetime | None]:
    """Follow a listing page by page, asking for each page with its stored ETag.

    Pages are chained only through the ``Link: rel="next"`` URL GitHub hands
    back. ``previous`` maps each page URL to ``{"etag", "next", "keys"}`` from
    the last run. A page that answers ``304`` is unchanged: it costs no quota,
    its ``keys`` (the small per-item records made by ``key``) are reused, and so
    is its stored next URL. Never a partial list: any failure propagates.

    Returns ``(fresh, reused, pages, listed_at)``: the items of pages that came
    back with a body, the stored keys of pages that did not, the page map to
    keep, and GitHub's clock at the first page (``None`` if any page lacked a
    ``Date``).
    """
    fresh: list[dict] = []
    reused: list[list] = []
    pages: dict[str, dict] = {}
    dates: list[datetime | None] = []
    url: str | None = first_url
    while url:
        if url in pages:  # a Link loop would otherwise never end
            raise GitHubResponseError(f"GitHub's pagination loops back on {_endpoint(url)}")
        stored = previous.get(url) or {}
        response = _get(client, url, etag=stored.get("etag"))
        dates.append(_server_time(response))
        if response.status_code == 304:
            reused.extend(stored["keys"])
            pages[url] = stored
            url = stored.get("next")
            continue
        items = _json(response, list)
        fresh.extend(items)
        next_url = _next_url(response)
        pages[url] = {
            "etag": (response.headers or {}).get("ETag"),
            "next": next_url,
            "keys": [key(item) for item in items],
        }
        url = next_url
    return fresh, reused, pages, _earliest(dates)


def _earliest(moments: list[datetime | None]) -> datetime | None:
    """The earliest of some ``Date`` readings, or ``None`` if one is missing."""
    if not moments or any(moment is None for moment in moments):
        return None
    return min(moments)


def _list_all(client: Any, url: str) -> tuple[list[dict], datetime | None]:
    """Every item of a small listing (comments, reviews), with GitHub's clock at its start."""
    items: list[dict] = []
    dates: list[datetime | None] = []
    seen: set[str] = set()
    next_url: str | None = url
    while next_url:
        if next_url in seen:
            raise GitHubResponseError(f"GitHub's pagination loops back on {_endpoint(next_url)}")
        seen.add(next_url)
        response = _get(client, next_url)
        items.extend(_json(response, list))
        dates.append(_server_time(response))
        next_url = _next_url(response)
    return items, _earliest(dates)


# ---------------------------------------------------------------------------
# Row ids
# ---------------------------------------------------------------------------
def _repo_key(repo: str) -> str:
    """``owner/name`` in lower case, so a change of case does not duplicate rows."""
    return repo.strip().strip("/").lower()


def _issue_row_id(repo: str, number: int, is_pr: bool) -> str:
    return f"{'pr' if is_pr else 'issue'}:{repo}#{number}"


def _repo_of(row_id: str) -> str:
    """The ``owner/name`` part of any row id."""
    return row_id.split(":", 1)[1].split("#")[0].split("@")[0]


# ---------------------------------------------------------------------------
# Rendering (pure)
# ---------------------------------------------------------------------------
def _is_bot(item: dict) -> bool:
    return (item.get("user") or {}).get("type") == "Bot"


def _login(item: dict) -> str:
    return (item.get("user") or {}).get("login") or "ghost"


def _header_lines(issue: dict) -> list[str]:
    """The ``Key: value`` lines at the top of an issue or pull request document.

    Uses only fields of the issue object that the issues listing returns, so
    ``_fingerprint`` can tell from the listing alone whether the document
    would read differently (a milestone or label renamed, a user renamed).
    """
    is_pr = "pull_request" in issue
    state = issue.get("state") or "open"
    merged = bool((issue.get("pull_request") or {}).get("merged_at"))
    if merged:
        state = "merged"
    elif issue.get("state_reason") and state == "closed":
        state = f"closed ({issue['state_reason'].replace('_', ' ')})"
    lines = [f"Type: {'pull request' if is_pr else 'issue'}", f"State: {state}"]
    if issue.get("draft"):
        lines.append("Draft: yes")
    lines.append(f"Author: {_login(issue)}")
    assignees = sorted(a["login"] for a in issue.get("assignees") or [] if a.get("login"))
    if assignees:
        lines.append(f"Assignees: {', '.join(assignees)}")
    labels = sorted(label["name"] for label in issue.get("labels") or [] if label.get("name"))
    if labels:
        lines.append(f"Labels: {', '.join(labels)}")
    milestone = (issue.get("milestone") or {}).get("title")
    if milestone:
        lines.append(f"Milestone: {milestone}")
    return lines


def _fingerprint(lines: list[str], include_bots: bool) -> str:
    """A short, stable hash of an issue's header lines and the bot setting.

    The bot setting is part of it so that changing ``include_bots`` renders
    every issue again with or without the bot comments.
    """
    text = "\n".join([*lines, f"bots: {include_bots}"])
    return hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]


def _conversation(items: list[dict], include_bots: bool) -> list[dict]:
    """Comments or reviews to render: bots left out unless asked for."""
    return [item for item in items if include_bots or not _is_bot(item)]


def _text(value: Any) -> str:
    return (value or "").strip()


def _in_order(items: list[dict]) -> list[dict]:
    """Comments and reviews oldest first, by id, whatever order they came in."""
    return sorted(items, key=lambda item: item.get("id") or 0)


def _render_issue(issue: dict, comments: list[dict]) -> str:
    """Render an issue and its comments as one text document.

    Deterministic for unchanged input and free of timestamps and counters, so
    an unchanged issue keeps its content hash and is not re-cognified. The
    title is not repeated here; it is the row's title.
    """
    sections = ["\n".join(_header_lines(issue))]
    if _text(issue.get("body")):
        sections.append(_text(issue["body"]))
    if comments:
        lines = [f"- {_login(c)}: {_text(c.get('body'))}" for c in _in_order(comments)]
        sections.append("Comments:\n" + "\n".join(lines))
    return "\n\n".join(sections)


def _render_pull(
    issue: dict,
    pull: dict,
    comments: list[dict],
    reviews: list[dict],
    review_comments: list[dict],
    files: list[dict],
) -> str:
    """Render a pull request: the issue fields, branches, reviews and changed files.

    No patches: a file is its path and line counts. At most ``_MAX_FILES``
    files are listed, followed by how many more the pull request changes;
    that number moves only when the files do, so it is not a volatile counter.
    """
    header = _header_lines(issue)
    base = (pull.get("base") or {}).get("ref")
    head = (pull.get("head") or {}).get("ref")
    if base and head:
        header.append(f"Branch: {head} -> {base}")
    sections = ["\n".join(header)]
    if _text(issue.get("body")):
        sections.append(_text(issue["body"]))
    if comments:
        lines = [f"- {_login(c)}: {_text(c.get('body'))}" for c in _in_order(comments)]
        sections.append("Comments:\n" + "\n".join(lines))
    # A plain "commented" review with no text only wraps line comments, which
    # are listed below; approvals and change requests count even when empty.
    shown = [r for r in _in_order(reviews) if _text(r.get("body")) or r.get("state") != "COMMENTED"]
    if shown:
        lines = []
        for review in shown:
            state = (review.get("state") or "").lower().replace("_", " ")
            body = _text(review.get("body"))
            lines.append(f"- {_login(review)} ({state}){': ' + body if body else ''}")
        sections.append("Reviews:\n" + "\n".join(lines))
    if review_comments:
        lines = [
            f"- {_login(c)} on {c.get('path') or '?'}: {_text(c.get('body'))}"
            for c in _in_order(review_comments)
        ]
        sections.append("Review comments:\n" + "\n".join(lines))
    if files:
        lines = [
            f"- {f['filename']} (+{f.get('additions', 0)}/-{f.get('deletions', 0)})"
            for f in sorted(files[:_MAX_FILES], key=lambda f: f["filename"])
        ]
        more = max(int(pull.get("changed_files") or len(files)) - len(lines), 0)
        if more:
            lines.append(f"- ... and {more} more files")
        sections.append("Changed files:\n" + "\n".join(lines))
    return "\n\n".join(sections)


def _render_repo(repo: dict, readme: str | None) -> str:
    """Render a repository: description, topics and settings, then its README."""
    lines = []
    if _text(repo.get("description")):
        lines.append(f"Description: {_text(repo['description'])}")
    if repo.get("topics"):
        lines.append(f"Topics: {', '.join(sorted(repo['topics']))}")
    if _text(repo.get("homepage")):
        lines.append(f"Homepage: {_text(repo['homepage'])}")
    if repo.get("language"):
        lines.append(f"Language: {repo['language']}")
    if repo.get("default_branch"):
        lines.append(f"Default branch: {repo['default_branch']}")
    if repo.get("archived"):
        lines.append("Archived: yes")
    sections = ["\n".join(lines)] if lines else []
    if _text(readme):
        sections.append("README:\n" + _text(readme))
    return "\n\n".join(sections)


def _render_release(release: dict) -> str:
    lines = [f"Tag: {release.get('tag_name') or ''}"]
    if release.get("prerelease"):
        lines.append("Pre-release: yes")
    if release.get("draft"):
        lines.append("Draft: yes")
    sections = ["\n".join(lines)]
    if _text(release.get("body")):
        sections.append(_text(release["body"]))
    return "\n\n".join(sections)


def _render_commit(commit: dict) -> str:
    detail = commit.get("commit") or {}
    author = (detail.get("author") or {}).get("name") or _login({"user": commit.get("author")})
    return f"Commit: {commit['sha'][:7]}\nAuthor: {author}\n\n{_text(detail.get('message'))}"


def _row(row_id: str, url: str, title: str, content: str) -> dict[str, Any]:
    return {"id": row_id, "url": url, "title": title.strip(), "content": content, "_deleted": False}


def _deleted_row(row_id: str) -> dict[str, Any]:
    """Build a minimal row that instructs dlt to hard-delete a document by id."""
    return {"id": row_id, "_deleted": True}


# ---------------------------------------------------------------------------
# Per-repository reads
# ---------------------------------------------------------------------------
def _sweep_key(issue: dict) -> list:
    """What is kept per issue of a sweep page: its number and whether it is a PR."""
    return [issue["number"], "pull_request" in issue]


def _change_key(issue: dict) -> list:
    return [issue["number"], "pull_request" in issue, issue.get("updated_at") or ""]


def _fetch_repo(client: _Client, repo: str, previous: dict, include_readme: bool) -> dict:
    """The repository object and README, or ``{"unchanged": True, ...}``.

    Both are asked for with their ETags. The repository's ETag changes on every
    push, and README text is not kept in state, so the README is fetched in
    full whenever the repository object changed. Only when both are unchanged
    (a repository without README stays without one) is the repository document
    left as it is. A selected repository that cannot be read stops the run.
    """
    try:
        response = _get(client, _url(f"/repos/{repo}"), etag=previous.get("repo_etag"))
    except GitHubAPIError as exc:
        if exc.status in (403, 404) and not exc.rate_limited:
            detail = f"repository {repo} does not exist or the token cannot read it"
            raise GitHubAPIError(exc.status, exc.endpoint, detail) from None
        raise
    repo_same = response.status_code == 304

    readme_response = None
    # Switching include_readme off must take the README out of the document.
    readme_same = previous.get("readme_etag") is None
    if include_readme:
        # "" marks "no README last time"; None means it was never looked at.
        stored = previous.get("readme_etag")
        readme_response = _get(
            client,
            _url(f"/repos/{repo}/readme"),
            etag=stored if repo_same and stored else None,
            accept=_RAW,
            ok=(200, 404),
        )
        readme_same = readme_response.status_code == 304 or (
            readme_response.status_code == 404 and stored == ""
        )
    if repo_same and readme_same:
        return {
            "unchanged": True,
            **{k: previous.get(k) for k in ("repo_etag", "readme_etag", "default_branch")},
        }

    if repo_same:
        # The README changed but the repository did not: its fields are needed.
        response = _get(client, _url(f"/repos/{repo}"))
    body = _json(response, dict)
    if (body.get("full_name") or repo).lower() != repo:
        logger.warning(
            "GitHub: %s was renamed to %s; update repos= to the new name.",
            repo,
            body.get("full_name"),
        )
    readme, readme_etag = None, None
    if readme_response is not None:
        if readme_response.status_code == 200:
            readme = readme_response.text
            readme_etag = (readme_response.headers or {}).get("ETag") or None
        else:
            readme_etag = ""
    return {
        "unchanged": False,
        "object": body,
        "readme": readme,
        "repo_etag": (response.headers or {}).get("ETag"),
        "readme_etag": readme_etag,
        "default_branch": body.get("default_branch"),
    }


def _commit_date(commit: dict) -> str:
    return ((commit.get("commit") or {}).get("committer") or {}).get("date") or ""


def _is_gone(client: _Client, repo: str, number: int) -> bool:
    """True when a number missing from the sweep is gone for good. Errors propagate.

    ``410`` (deleted) and a ``301`` whose ``Location`` is an issue in *another*
    repository (transferred; seen live as ``/repos/<owner>/<other>/issues/<n>``)
    mean gone. A renamed repository answers ``301`` too, pointing back at
    itself, so any other ``Location`` does not count, and neither does ``200``.
    """
    response = _get(
        client,
        _url(f"/repos/{repo}/issues/{number}"),
        follow_redirects=False,
        ok=(301, 410, 200),
    )
    if response.status_code == 410:
        return True
    if response.status_code == 301:
        target = _transfer_target((response.headers or {}).get("Location") or "")
        return bool(target and target != repo)
    return False


def _transfer_target(location: str) -> str | None:
    """``owner/name`` (lower case) of an issue URL ``.../repos/<owner>/<name>/issues/<n>``."""
    path = location.removeprefix(_API_BASE).split("?")[0].strip("/").split("/")
    if len(path) == 5 and path[0] == "repos" and path[3] == "issues" and path[4].isdigit():
        return f"{path[1]}/{path[2]}".lower()
    return None


def _render_one(
    client: _Client, plan: dict, number: int, include_bots: bool
) -> tuple[dict[str, Any], bool]:
    """Fetch and render one changed issue or pull request: ``(row, settled)``.

    Records the issue's new fingerprint only once it rendered, so a failure
    anywhere in here leaves the caller free to keep the issue for a retry.
    """
    repo = plan["repo"]
    issue = plan["issues"].get(number)
    # The issue's body and header come from a listing made at listed_at.
    body_at = plan["listed_at"]
    if issue is None:  # only reused sweep pages mention it: fetch it whole
        response = _get(client, _url(f"/repos/{repo}/issues/{number}"))
        issue, body_at = _json(response, dict), _server_time(response)
    is_pr = plan["kinds"][number]
    comments, fetched_at = _list_all(
        client, _url(f"/repos/{repo}/issues/{number}/comments", {"per_page": _PER_PAGE})
    )
    comments = _conversation(comments, include_bots)
    if is_pr:
        pull = _json(_get(client, _url(f"/repos/{repo}/pulls/{number}")), dict)
        reviews, _ = _list_all(
            client, _url(f"/repos/{repo}/pulls/{number}/reviews", {"per_page": _PER_PAGE})
        )
        review_comments, _ = _list_all(
            client, _url(f"/repos/{repo}/pulls/{number}/comments", {"per_page": _PER_PAGE})
        )
        # One page only: the total comes from changed_files, not from paging.
        files = _json(
            _get(client, _url(f"/repos/{repo}/pulls/{number}/files", {"per_page": _MAX_FILES})),
            list,
        )
        content = _render_pull(
            issue,
            pull,
            comments,
            _conversation(reviews, include_bots),
            _conversation(review_comments, include_bots),
            files,
        )
    else:
        content = _render_issue(issue, comments)
    plan["current_prints"][str(number)] = _fingerprint(_header_lines(issue), include_bots)
    # Skip it next run only if no later change can share its timestamp. The
    # document is as old as the oldest of its parts: the listing (or GET)
    # that gave the body, or the comments.
    settled = _is_settled(plan["stamps"].get(number, ""), _earliest([body_at, fetched_at]))
    row = _row(
        _issue_row_id(repo, number, is_pr),
        issue.get("html_url") or "",
        issue.get("title") or "",
        content,
    )
    return row, settled


# ---------------------------------------------------------------------------
# Sync (pure given a session + state dict, so it is unit-testable)
# ---------------------------------------------------------------------------
class _Run:
    """One sync over the selected repositories; ``state`` changes only at the end.

    State: ``known_ids`` (row ids emitted and current), ``retry`` (issue rows
    kept after a failure), ``unreadable`` (row id -> ``{stamp, runs, since}`` of
    issues answering 403/404, or missing from the sweep with no clear answer;
    ``since`` is GitHub's clock at the first counted run), ``given_up`` (row id
    -> ``{stamp, at}``: the ``updated_at`` at which a listed, unreadable issue
    was forgotten, and when it was last asked) and, per repository, its ETags
    and page maps, the
    ``cursor``, ``seen`` (issues inside the overlap whose copy is final),
    ``fingerprints`` (number -> header hash) and the commit cursor and shas.
    """

    def __init__(
        self,
        client: _Client,
        state: dict,
        stats: dict[str, int],
        *,
        include_commits: bool,
        include_bots: bool,
        include_readme: bool,
        now: datetime | None = None,
    ):
        self.client = client
        self.state = state
        self.stats = stats
        self.include_commits = include_commits
        self.include_bots = include_bots
        self.include_readme = include_readme
        self.known_ids: set[str] = set(state.get("known_ids", []))
        self.unreadable: dict[str, dict] = _load_unreadable(state.get("unreadable", {}))
        self.given_up: dict[str, dict] = _load_given_up(state.get("given_up", {}))
        # What a run that stops early commits instead: only finished runs count.
        self.unreadable_before = {row: dict(e) for row, e in self.unreadable.items()}
        self.retry: set[str] = set(state.get("retry", []))
        self.kept: set[str] = set()  # listed issues whose fetch failed this run
        self.unclear: set[str] = set()  # missing from the sweep, not shown to be gone
        self.rendered: set[str] = set()  # issue rows emitted this run
        # 403/404s are counted toward giving up only once the run shows that items
        # of the same kind in the same repository can be read:
        # (repo, kind) -> [(row id, stamp)], see settle().
        self.unread: dict[tuple[str, str], list[tuple[str, str]]] = {}
        self.counted: set[str] = set()  # rows counted as unreadable in this run
        self.read_kinds: set[tuple[str, str]] = set()  # (repo, kind) fetched successfully
        self.reasks: set[str] | None = None  # forgotten rows asked again this run
        self.reask_failed: set[str] = set()
        self.now_override = now
        self._now: datetime | None = None
        self.may_give_up = True
        self.emitted: set[str] = set()  # every row id emitted this run
        self.plans: list[dict] = []
        self.outage = 0
        self.listed = False

    def clock(self) -> datetime:
        """The clock of the give-up and re-ask times (see ``_run_clocks``), read once
        GitHub has answered."""
        if self._now is None:
            if self.now_override:
                now, self.may_give_up = self.now_override, True
            else:
                now, self.may_give_up = _run_clocks(self.client)
            self._now = now.replace(microsecond=0)
            self._clamp_times()
        return self._now

    def _clamp_times(self) -> None:
        """Bring a time stored by a run whose clock was ahead back to now.

        Such a time (written before GitHub's clock was used for these) would hold
        a re-ask off, or start a 24 hours, as far in the future as that clock was
        ahead.
        """
        latest = self._now + _CLOCK_TOLERANCE
        for entries, key in (
            (self.unreadable, "since"),
            (self.unreadable_before, "since"),
            (self.given_up, "at"),
        ):
            for row_id, entry in entries.items():
                if entry.get(key) and _parse(entry[key]) > latest:
                    if entries is not self.unreadable_before:
                        logger.info("GitHub: %s had %s in the future; reset to now.", row_id, key)
                    entry[key] = _stamp(self._now)

    def stop_if_systemic(self, exc: GitHubAPIError) -> None:
        if self.client.out_of_time:
            raise _CutShortError("rate_limit" if exc.rate_limited else "unavailable") from None
        if exc.rate_limited:
            raise _CutShortError("rate_limit") from None
        if exc.status == 401 and self.rendered:
            raise _CutShortError("auth") from None

    # -- 1. List ---------------------------------------------------------------
    def list(self, repos: list[str]) -> None:
        previous_repos: dict = self.state.get("repos", {})
        self.plans = [self._plan_repo(repo, previous_repos.get(repo) or {}) for repo in repos]
        self.listed = True

    def _plan_repo(self, repo: str, previous: dict) -> dict[str, Any]:
        """Every listing and check of one repository, nothing yielded."""
        include_bots = self.include_bots
        if previous.get("include_bots") != include_bots:
            # Stored fingerprints and pages were made with the other bot setting:
            # drop them, so every issue is compared (and rendered) afresh.
            previous = {
                k: v for k, v in previous.items() if k not in ("fingerprints", "sweep_pages")
            }
        fingerprints: dict[str, str] = previous.get("fingerprints", {})
        seen: dict[str, str] = previous.get("seen", {})
        cursor: str = previous.get("cursor", "")

        repo_info = _fetch_repo(self.client, repo, previous, self.include_readme)
        if self.reasks is None:
            # Forgotten issues not edited since are asked again after a day, after
            # everything else; all that are due, as they are bounded by what was
            # forgotten, one request a day each.
            self.reasks = {
                row
                for row, entry in self.given_up.items()
                if _parse(entry["at"]) <= self.clock() - _REASK_AFTER
            }

        # The sweep: every issue and pull request that exists right now, oldest first.
        sweep_url = _url(
            f"/repos/{repo}/issues",
            {"state": "all", "sort": "created", "direction": "asc", "per_page": _PER_PAGE},
        )
        fresh, reused, sweep_pages, swept_at = _list_pages(
            self.client, sweep_url, previous.get("sweep_pages", {}), _sweep_key
        )
        current_prints = {str(number): fingerprints.get(str(number), "") for number, _ in reused}
        # number -> is it a pull request, for every issue in the sweep
        kinds: dict[int, bool] = dict(map(tuple, reused))
        issues_by_number = {}
        for issue in fresh:
            number = issue["number"]
            kinds[number] = "pull_request" in issue
            issues_by_number[number] = issue
            current_prints[str(number)] = _fingerprint(_header_lines(issue), include_bots)

        # The change listing: everything updated since a little before the cursor.
        # On a first sync every issue is new, so the sweep already says it all.
        stamps: dict[int, str] = {}
        change_pages: dict = {}
        if cursor:
            since = _stamp(_parse(cursor) - _CURSOR_OVERLAP)
            change_url = _url(
                f"/repos/{repo}/issues",
                {
                    "state": "all",
                    "since": since,
                    "sort": "updated",
                    "direction": "asc",
                    "per_page": _PER_PAGE,
                },
            )
            changed_fresh, changed_reused, change_pages, changes_at = _list_pages(
                self.client, change_url, previous.get("change_pages", {}), _change_key
            )
            listed_at = _earliest([swept_at, changes_at])
            stamps.update({number: when for number, _, when in changed_reused})
            for issue in changed_fresh:
                stamps[issue["number"]] = issue.get("updated_at") or ""
                issues_by_number.setdefault(issue["number"], issue)
        else:
            stamps.update(
                {
                    number: issue.get("updated_at") or ""
                    for number, issue in issues_by_number.items()
                }
            )
            listed_at = swept_at

        def row_of(number: int) -> str:
            return _issue_row_id(repo, number, kinds[number])

        settled = {number for number, when in stamps.items() if seen.get(str(number)) == when}
        changed = set(stamps) - settled
        # A header that would read differently (milestone or label renamed, bots
        # setting changed), or an issue with no fingerprint yet, is rendered too,
        # and so is every issue kept for a retry.
        changed |= {
            number
            for number in kinds
            if fingerprints.get(str(number)) != current_prints[str(number)]
            or row_of(number) in self.retry
        }
        changed &= set(kinds)

        current_ids = {row_of(number) for number in kinds}
        # A forgotten unreadable issue is asked again once its updated_at moves,
        # or when it is due again (``self.reasks``).
        excluded = 0
        asked = []
        for number in sorted(kinds):
            row = row_of(number)
            if row not in self.given_up:
                continue
            if number in stamps and stamps[number] != self.given_up[row]["stamp"]:
                del self.given_up[row]
                changed.add(number)
                continue
            changed.discard(number)
            current_ids.discard(row)
            if row in self.reasks:
                asked.append(number)
            else:
                excluded += 1
        fresh_numbers = sorted(n for n in changed if row_of(n) not in self.retry)
        retried = sorted(n for n in changed if row_of(n) in self.retry)
        order = fresh_numbers + retried + asked

        # Numbers that were documents before and are not in the sweep any more.
        missing = sorted(
            row_id
            for row_id in self.known_ids
            if row_id.startswith(("issue:", "pr:"))
            and _repo_of(row_id) == repo
            and row_id not in current_ids
            and row_id not in self.given_up
        )
        for row_id in missing:
            if not self._confirm_gone(repo, row_id):
                current_ids.add(row_id)  # not shown to be gone: keep it, ask again next run

        # Releases: unchanged pages keep their rows, changed pages are re-emitted.
        fresh_releases, reused_releases, release_pages, _ = _list_pages(
            self.client,
            _url(f"/repos/{repo}/releases", {"per_page": _PER_PAGE}),
            previous.get("release_pages", {}),
            lambda release: [release["id"]],
        )
        release_ids = {f"release:{repo}@{rid}" for (rid,) in reused_releases}
        release_ids |= {f"release:{repo}@{release['id']}" for release in fresh_releases}

        # Commits (opt-in): append-only. GitHub lists them newest first; the
        # listing starts a little before the newest commit date seen and, like the
        # others, is asked for with its stored ETags.
        include_commits = self.include_commits
        commit_shas: list[str] = list(previous.get("commit_shas", [])) if include_commits else []
        commit_cursor: str = previous.get("commit_cursor", "") if include_commits else ""
        commit_pages: dict = {}
        new_commits: list[dict] = []
        if include_commits and repo_info.get("default_branch"):
            params = {"sha": repo_info["default_branch"], "per_page": _PER_PAGE}
            if commit_cursor:
                params["since"] = _stamp(_parse(commit_cursor) - _CURSOR_OVERLAP)
            listed, reused_commits, commit_pages, _ = _list_pages(
                self.client,
                _url(f"/repos/{repo}/commits", params),
                previous.get("commit_pages", {}),
                lambda commit: [commit["sha"], _commit_date(commit)],
            )
            known_shas = set(commit_shas)
            new_commits = [c for c in reversed(listed) if c["sha"] not in known_shas]
            commit_shas += [c["sha"] for c in new_commits]
            dates = [_commit_date(c) for c in listed] + [date for _, date in reused_commits]
            commit_cursor = max([commit_cursor, *dates])

        current_ids |= release_ids
        current_ids |= {f"commit:{repo}@{sha}" for sha in commit_shas}
        current_ids.add(f"repo:{repo}")
        self.stats["scanned"] += len(kinds)
        self.stats["skipped"] += len(kinds) - len(order) - excluded
        return {
            "repo": repo,
            "previous": previous,
            "repo_info": repo_info,
            "issues": issues_by_number,
            "kinds": kinds,
            "order": order,
            "stamps": stamps,
            "settled": settled,
            "listed_at": listed_at,
            "fresh_releases": fresh_releases,
            "new_commits": new_commits,
            "current_ids": current_ids,
            "current_prints": current_prints,
            "rendered": set(),  # numbers rendered this run
            "settled_now": set(),  # of those, the ones whose copy is final
            "done": False,
            "next_state": {
                "repo_etag": repo_info.get("repo_etag"),
                "readme_etag": repo_info.get("readme_etag"),
                "default_branch": repo_info.get("default_branch"),
                "sweep_pages": sweep_pages,
                "change_pages": change_pages,
                "release_pages": release_pages,
                "commit_shas": commit_shas,
                "commit_cursor": commit_cursor,
                "commit_pages": commit_pages,
                "include_bots": include_bots,
                # Filled in by _render_repo once the issues are fetched.
                "cursor": max([cursor, *stamps.values()]),
                "fingerprints": current_prints,
                "seen": {},
            },
        }

    def _confirm_gone(self, repo: str, row_id: str) -> bool:
        """Ask once about a number missing from the sweep; True only when it is gone.

        Any answer but "gone" keeps the document; a clear one (``200``, a ``301``
        that is not a transfer) counts toward giving it up, and so does a ``403``
        or ``404`` where access to that kind is shown to work (see ``settle``).
        An error that outlasts the retries counts toward nothing.
        """
        try:
            if _is_gone(self.client, repo, int(row_id.rsplit("#", 1)[1])):
                return True
            why, status = "answering, but not gone", 200
        except GitHubAPIError as exc:
            if exc.rate_limited or exc.status == 401 or self.client.out_of_time:
                raise
            why, status = str(exc), exc.status
            self.stats["failed"] += 1
        if status == 200:
            self._count_unreadable(row_id, "missing")
        elif status in (403, 404):
            self.unread.setdefault((repo, row_id.split(":", 1)[0]), []).append((row_id, "missing"))
        self.unclear.add(row_id)
        logger.warning("GitHub: kept %s, missing from the listing (%s).", row_id, why)
        return False

    # -- 2. Render -------------------------------------------------------------
    def render(self) -> Iterator[dict[str, Any]]:
        for plan in self.plans:
            yield from self._render_repo(plan)
            plan["done"] = True
        for repo, kind in sorted(self.unread):
            self.settle(repo, kind)

    def _render_repo(self, plan: dict) -> Iterator[dict[str, Any]]:
        repo = plan["repo"]
        for number in plan["order"]:
            row_id = _issue_row_id(repo, number, plan["kinds"][number])
            try:
                row, is_settled = _render_one(self.client, plan, number, self.include_bots)
            except GitHubAPIError as exc:
                self._failed(plan, number, row_id, exc)
                continue
            if row_id not in self.retry | self.reasks:
                self.outage = 0
            if self.given_up.pop(row_id, None) is not None:
                logger.info("GitHub: %s is readable again.", row_id)
                plan["current_ids"].add(row_id)
            self.unreadable.pop(row_id, None)
            self.read_kinds.add((repo, row_id.split(":", 1)[0]))
            plan["rendered"].add(number)
            if is_settled:
                plan["settled_now"].add(number)
            self.rendered.add(row_id)
            yield self._emit(row)

        info = plan["repo_info"]
        if not info["unchanged"]:
            repo_obj = info["object"]
            yield self._emit(
                _row(
                    f"repo:{repo}",
                    repo_obj.get("html_url") or "",
                    repo_obj.get("full_name") or repo,
                    _render_repo(repo_obj, info["readme"]),
                )
            )
        for release in plan["fresh_releases"]:
            yield self._emit(
                _row(
                    f"release:{repo}@{release['id']}",
                    release.get("html_url") or "",
                    release.get("name") or release.get("tag_name") or "",
                    _render_release(release),
                )
            )
        for commit in plan["new_commits"]:
            message = _text((commit.get("commit") or {}).get("message"))
            yield self._emit(
                _row(
                    f"commit:{repo}@{commit['sha']}",
                    commit.get("html_url") or "",
                    message.splitlines()[0] if message else commit["sha"][:7],
                    _render_commit(commit),
                )
            )

        cursor = plan["next_state"]["cursor"]
        window_start = _stamp(_parse(cursor) - _CURSOR_OVERLAP) if cursor else ""
        settled = plan["settled"] | plan["settled_now"]
        plan["next_state"]["seen"] = {
            str(number): when
            for number, when in plan["stamps"].items()
            if number in settled and when >= window_start
        }

    def _emit(self, row: dict[str, Any]) -> dict[str, Any]:
        self.emitted.add(row["id"])
        return row

    def _failed(self, plan: dict, number: int, row_id: str, exc: GitHubAPIError) -> None:
        """Keep an issue that failed on its own, or stop the run if GitHub as a whole is failing."""
        if row_id not in self.reasks:
            self.stats["failed"] += 1
        self.stop_if_systemic(exc)
        if exc.status == 401:
            raise exc  # nothing rendered yet: the token is bad
        if row_id in self.reasks:
            # Still unreadable: it stays forgotten and is asked again in a day. Its
            # document is already gone, so this is no failure of the run.
            self.stats["reask_failed"] = self.stats.get("reask_failed", 0) + 1
            logger.info("GitHub: %s is still unreadable (%s); asked again in a day.", row_id, exc)
            self.reask_failed.add(row_id)
            return
        self.kept.add(row_id)
        logger.warning("GitHub: kept %s for a retry next run (%s).", row_id, exc)
        if exc.status in (403, 404):
            # Still listed, so not gone: hidden from this token, or a passing
            # failure. settle() decides which once every repository is rendered.
            stamp = (
                plan["stamps"].get(number)
                or (plan["issues"].get(number) or {}).get("updated_at")
                or (self.unreadable.get(row_id) or {}).get("stamp", "")
            )
            key = (plan["repo"], row_id.split(":", 1)[0])
            self.unread.setdefault(key, []).append((row_id, stamp))
        elif row_id not in self.retry:
            self.outage += 1
            if self.outage >= _OUTAGE_STREAK:
                raise _CutShortError("unavailable")

    def _count_unreadable(self, row_id: str, stamp: str) -> None:
        """One more run in which ``row_id`` could not be read at ``stamp``."""
        entry = self.unreadable.get(row_id)
        if entry and entry["stamp"] == stamp:
            since = entry.get("since") or _stamp(self.clock())
            self.unreadable[row_id] = {**entry, "runs": entry["runs"] + 1, "since": since}
        else:
            self.unreadable[row_id] = {"stamp": stamp, "runs": 1, "since": _stamp(self.clock())}
        self.counted.add(row_id)

    def settle(self, repo: str, kind: str) -> None:
        """Count this run's 403/404s of one kind in one repository where access is shown to work.

        A 403 or 404 means the item is hidden from this token, or that GitHub is
        failing or the token lacks a permission (a fine-grained token without
        "Pull requests: read" lists pull requests but cannot read them; a
        private repository it cannot see answers 404). Only the first is a
        reason to forget it. So it counts only if another item of the same kind
        in the same repository was read in this run: access can differ per
        repository (an organization's token policy, an app installed on some
        repositories only). In steady state the others are unchanged and not
        fetched, so one of them is fetched as a probe; if the probe fails too,
        nothing counts. One of them may be hidden from the token itself, so up to
        ``_PROBE_TRIES`` are tried.
        """
        pending = self.unread.pop((repo, kind), [])
        if not pending:
            return
        if (repo, kind) not in self.read_kinds:
            numbers = self._probe_targets(repo, kind)
            probe = self._probe(kind, repo, numbers)
            if probe is False:
                self.stats["failed_permission"] = self.stats.get("failed_permission", 0) + len(
                    pending
                )
                if not numbers:
                    logger.warning(
                        "%s %s: every fetch answered 403/404 this run and there is no other %s "
                        "in %s to check access with; nothing counts toward forgetting it.",
                        repo,
                        _KIND_NAMES[kind],
                        _KIND_NAME[kind],
                        repo,
                    )
                else:
                    logger.warning(
                        "%s %s: every fetch answered 403/404 this run; "
                        "check the token's permissions.",
                        repo,
                        _KIND_NAMES[kind],
                    )
            if not probe:
                return
        for row_id, stamp in pending:
            self._count_unreadable(row_id, stamp)

    def _probe_targets(self, repo: str, kind: str) -> list[int]:
        """The newest items of ``kind`` in ``repo`` not fetched this run and not given
        up, at most ``_PROBE_TRIES``.

        Items fetched this run, retries (every unreadable one) included, are in
        the plan's order; a number missing from the sweep is in no plan.
        """
        plan = next(plan for plan in self.plans if plan["repo"] == repo)
        in_order = set(plan["order"])
        numbers = [
            number
            for number, is_pr in plan["kinds"].items()
            if is_pr == (kind == "pr")
            and number not in in_order
            and _issue_row_id(repo, number, is_pr) not in self.given_up
        ]
        return sorted(numbers, reverse=True)[:_PROBE_TRIES]

    def _probe(self, kind: str, repo: str, numbers: list[int]) -> bool | None:
        """Read unchanged items of ``kind`` in turn: True at the first that reads.

        A pull request is read through ``/pulls/N`` (what a token without pull
        request access is refused), an issue through its comments. A 403/404
        moves on to the next. False when every one answers 403/404, or there is
        none to try; None when GitHub is failing (5xx, no answer), which proves
        nothing.
        """
        for number in numbers:
            self.stats[f"probe_{kind}"] = self.stats.get(f"probe_{kind}", 0) + 1
            if kind == "pr":
                url = _url(f"/repos/{repo}/pulls/{number}")
            else:
                url = _url(f"/repos/{repo}/issues/{number}/comments", {"per_page": 1})
            try:
                _get(self.client, url)
            except GitHubAPIError as exc:
                self.stop_if_systemic(exc)
                if exc.status == 401:
                    raise
                if exc.status not in (403, 404):
                    return None
                continue
            return True
        return False

    # -- 3. Delete and 4. Commit ------------------------------------------------
    def finish(self) -> Iterator[dict[str, Any]]:
        current_ids: set[str] = set()
        for plan in self.plans:
            current_ids |= plan["current_ids"]
        now = self.clock()
        for row_id, entry in sorted(self.unreadable.items()):
            # Only an item counted in this run, three times, over at least a day.
            if (
                not self.may_give_up
                or row_id not in self.counted
                or entry["runs"] < _GIVE_UP_RUNS
                or _parse(entry["since"]) > now - _GIVE_UP_AFTER
            ):
                continue
            if row_id in self.kept:
                self.given_up[row_id] = {"stamp": entry["stamp"], "at": _stamp(now)}
                self.kept.discard(row_id)
            elif row_id in self.unclear:
                self.unclear.discard(row_id)
            else:
                continue
            logger.warning(
                "GitHub: %s could not be read in %d runs since %s; its document is forgotten.",
                row_id,
                entry["runs"],
                entry["since"],
            )
            current_ids.discard(row_id)
            self.stats["given_up"] += 1
        for row_id in self.reask_failed:
            self.given_up[row_id]["at"] = _stamp(now)
        deleted = sorted(self.known_ids - current_ids)
        for row_id in deleted:
            yield _deleted_row(row_id)
        self.stats["deleted"] = len(deleted)

        swept = {
            _issue_row_id(plan["repo"], number, is_pr)
            for plan in self.plans
            for number, is_pr in plan["kinds"].items()
        }
        self.state["known_ids"] = sorted(current_ids)
        self.state["repos"] = {plan["repo"]: plan["next_state"] for plan in self.plans}
        self._commit_items(self.kept, self.unreadable)
        self.state["given_up"] = {
            row: entry for row, entry in sorted(self.given_up.items()) if row in swept
        }

    def commit_progress(self) -> None:
        """After a clean stop: keep what was rendered, delete nothing, relist next run.

        A repository finished before the stop commits its new state. The others
        keep their old cursors and ETags, so the next run lists the same changes
        again; the fingerprints and ``seen`` entries of the issues rendered here
        make it skip those. Nothing this run saw counts toward giving an item up,
        nothing new is forgotten, and no forgotten item is marked as asked.
        """
        if not self.listed:
            return  # stopped while listing: nothing moved
        repos = {}
        for plan in self.plans:
            if plan["done"]:
                repos[plan["repo"]] = plan["next_state"]
                continue
            kept = dict(plan["previous"])
            prints = dict(kept.get("fingerprints", {}))
            seen = dict(kept.get("seen", {}))
            for number in plan["rendered"]:
                prints[str(number)] = plan["current_prints"][str(number)]
            for number, stamp in plan["stamps"].items():
                if number in plan["settled_now"]:
                    seen[str(number)] = stamp
            kept.update(fingerprints=prints, seen=seen, include_bots=self.include_bots)
            repos[plan["repo"]] = kept
        self.state["known_ids"] = sorted(self.known_ids | self.emitted)
        self.state["repos"] = repos
        self._commit_items(
            self.kept | (self.retry - self.rendered),
            {row: e for row, e in self.unreadable_before.items() if row not in self.rendered},
        )
        self.state["given_up"] = dict(sorted(self.given_up.items()))

    def _commit_items(self, retry: set[str], unreadable: dict[str, dict]) -> None:
        self.state["retry"] = sorted(retry)
        self.state["unreadable"] = {
            row: {**entry, "since": entry.get("since") or _stamp(self.clock())}
            for row, entry in sorted(unreadable.items())
            if row in retry or row in self.unclear
        }
        self.stats["kept"] = len(retry) + len(self.unclear)


def _load_unreadable(entries: dict) -> dict[str, dict]:
    """``unreadable`` from state. An entry of an earlier version without ``since``
    gets one from the clock of the give-up times when next written; one with no counted run
    (``runs`` 0) is dropped."""
    return {row: dict(entry) for row, entry in entries.items() if entry.get("runs")}


def _load_given_up(entries: dict) -> dict[str, dict]:
    """``given_up`` from state. An earlier version kept only the stamp: such an
    entry is due to be asked again at once."""
    return {
        row: dict(entry) if isinstance(entry, dict) else {"stamp": entry, "at": _stamp(_EPOCH)}
        for row, entry in entries.items()
    }


def _run_clocks(client: _Client) -> tuple[datetime, bool]:
    """The clock of the give-up and re-ask times, and whether an item may be
    forgotten in this run.

    These times are measured on GitHub's clock (its first answer's Date) alone:
    a local clock that jumps ahead (an NTP step, a resumed VM) must not make a
    day pass in minutes. When the two clocks differ by more than
    ``_CLOCK_TOLERANCE`` one of them is wrong (a stale Date from a caching
    proxy, or the local clock), so nothing is forgotten in this run (counting
    goes on). Without a Date header the local clock is all there is, and a jump
    of it is not caught; that takes a proxy that strips the header. (Listings
    end on GitHub's Date readings of their own, see _list_pages.)
    """
    local = _local_now()
    server = client.server_now
    if server is None:
        logger.warning("GitHub: no Date header seen; the give-up times use the local clock.")
        return local, True
    if abs(server - local) > _CLOCK_TOLERANCE:
        logger.warning(
            "GitHub: its Date header and the local clock differ by %.0fs; no item is "
            "forgotten in this run.",
            abs((server - local).total_seconds()),
        )
        return server, False
    return server, True


def _local_now() -> datetime:
    return datetime.now(UTC)


def sync_repos(
    session: Any,
    state: dict,
    *,
    repos: list[str],
    include_commits: bool = False,
    include_bots: bool = False,
    include_readme: bool = True,
    check_active: Callable[[], None] | None = None,
    stats: dict[str, int] | None = None,
    now: datetime | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield changed documents since the last run, plus hard-delete markers.

    1. *List.* Per repository: the repository and README, the sweep of all
       issue numbers, the change listing, the releases and the commits; every
       number missing from the sweep is asked about. A failure propagates
       before a single row.
    2. *Render.* Each changed issue and pull request (fresh first, then
       retries, then forgotten ones due to be asked again), then the
       repository, release and commit rows that changed.
    3. *Delete.* A hard-delete marker for every known id that is no longer current.
    4. *Commit.* Only now is ``state`` written.

    A run stopped cleanly (a ``401`` after progress, a rate limit, an outage,
    the wait budget spent) keeps what it rendered and deletes nothing; the next
    run relists. ``now`` (for tests) sets the clock of the give-up times (``_run_clocks``).
    """
    if not repos:
        raise ValueError(
            "No GitHub repository selected; an empty selection would forget everything"
        )
    stats = {} if stats is None else stats
    for key in _STAT_KEYS:
        stats.setdefault(key, 0)
    client = session if isinstance(session, _Client) else _Client(session, check_active)
    run = _Run(
        client,
        state,
        stats,
        include_commits=include_commits,
        include_bots=include_bots,
        include_readme=include_readme,
        now=now,
    )
    try:
        try:
            run.list(repos)
            yield from run.render()
            yield from run.finish()
        except GitHubAPIError as exc:
            run.stop_if_systemic(exc)  # also a rate limit while listing
            raise
    except _CutShortError as cut:
        run.commit_progress()
        stats[f"failed_{cut.reason}"] = 1
        logger.warning("GitHub sync stopped early (%s); it resumes on the next run.", cut.reason)
        return
    logger.info(
        "GitHub: %d issue(s)/PR(s) rendered, %d deletion(s) across %d repo(s); "
        "kept %d item(s) for retry.",
        len(run.rendered),
        stats["deleted"],
        len(repos),
        stats["kept"],
    )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def github_source(
    token: str | None = None,
    repos: list[str] | None = None,
    include_commits: bool = False,
    include_bots: bool = False,
    include_readme: bool = True,
    service: Any = None,
    *,
    resource_name: str = GITHUB_TABLE_NAME,
    check_active: Callable[[], None] | None = None,
    session: Any = None,
):
    """Return a ``dlt`` resource that yields GitHub documents for ``remember``.

    Args:
        token: GitHub token, classic or fine-grained. Falls back to ``GITHUB_TOKEN``.
        repos: The repositories to ingest, as ``"owner/name"``.
        include_commits: Also ingest commit messages of each default branch.
        include_bots: Keep comments and reviews written by bots (Copilot,
            github-actions, ...). Issues and pull requests opened by a bot are
            ingested either way.
        include_readme: Put the README text into the repository document.
        service: Pre-built ``requests``-like session, mainly for tests; when
            omitted one is built from the token for each run and closed after it.
            ``session`` is accepted as an alias.
        resource_name: dlt resource (table) name, which also scopes the sync
            state. Give each token or selection its own name in a shared dataset.
        check_active: Optional host authorization checkpoint, called before every
            request and wait and around every row; it stops the sync by raising.

    Returns:
        A ``dlt`` resource with ``primary_key="id"``, ``write_disposition="merge"``
        and a ``_deleted`` hard-delete column. ``resource.cognee_sync_stats``
        holds the last run's counts.
    """
    if not (hasattr(dlt_utils, "PIPELINE_SCOPE_ATTR") and hasattr(dlt_utils, "guarded_rows")):
        raise RuntimeError(
            "The GitHub connector needs cognee's dlt_utils.PIPELINE_SCOPE_ATTR and "
            "guarded_rows (cognee>=1.6.2)."
        )
    _check_resource_name(resource_name)
    if service is not None and session is not None:
        raise ValueError("Pass service= or session=, not both")
    service = service if service is not None else session

    if isinstance(repos, str):
        repos = [repos]
    selected = list(dict.fromkeys(_repo_key(repo) for repo in repos or []))
    if not selected or any(repo.count("/") != 1 or not all(repo.split("/")) for repo in selected):
        raise ValueError('github_source requires repos=["owner/name", ...]')

    resolved_token = token or os.environ.get("GITHUB_TOKEN") or ""
    if service is None:
        if not resolved_token:
            raise ValueError("GitHub token required: pass token= or set GITHUB_TOKEN.")
        _check_token(resolved_token)
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        # Rows with _deleted=True are removed from the destination on merge,
        # which propagates the deletion through cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def github_documents():
        stats.clear()
        http = service if service is not None else _make_session(resolved_token)
        try:
            rows = sync_repos(
                _Client(http, check_active, resolved_token),
                dlt.current.resource_state(),
                repos=selected,
                include_commits=include_commits,
                include_bots=include_bots,
                include_readme=include_readme,
                stats=stats,
            )
            yield from dlt_utils.guarded_rows(rows, check_active)
        finally:
            if service is None:
                http.close()

    resource = github_documents()
    # Opt into the document ingestion path (row -> text document -> cognify).
    setattr(resource, DOCUMENT_SOURCE_ATTR, GITHUB_SOURCE_NAME)
    # Give the sync state its own dlt pipeline, apart from other sources.
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    resource.cognee_sync_stats = stats
    return resource


def _check_resource_name(name: Any) -> None:
    """A non-empty name of at most 64 characters that dlt keeps as it is.

    It names the table and scopes the state: an empty name would lose that
    scope, and two names dlt normalizes alike ("Foo-Bar", "foo_bar") would share
    a table.
    """
    if not isinstance(name, str) or not name:
        raise ValueError("resource_name must be a non-empty string")
    if len(name) > 64:
        raise ValueError("resource_name must be at most 64 characters")
    if NamingConvention().normalize_table_identifier(name) != name:
        raise ValueError("resource_name must be lowercase snake_case (letters, digits, _)")
