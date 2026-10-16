"""GitHub connector for cognee: a ``dlt`` source that turns GitHub repositories into memory.

Pulls repositories (description, topics, README), issues, pull requests,
releases and, opt-in, default-branch commit messages, incrementally and with
forget-on-delete. The source built here is handed directly to
:func:`cognee.remember`::

    import cognee
    from cognee_community_connector_github import github_source

    await cognee.remember(
        github_source(repos=["owner/name"]),  # GITHUB_TOKEN from env
        dataset_name="github",
        primary_key="id",
        write_disposition="merge",   # incremental upsert by row id
        max_rows_per_table=0,        # 0 = no row cap
    )

Design
------
* **Auth**: a personal access token (classic or fine-grained), sent as
  ``Authorization: Bearer``. The connector only issues ``GET`` requests.
* **Documents**: one per repository (``repo:<owner>/<name>``), issue
  (``issue:<owner>/<name>#<n>``), pull request (``pr:<owner>/<name>#<n>``),
  release (``release:<owner>/<name>@<id>``) and, opt-in, commit
  (``commit:<owner>/<name>@<sha>``). Comments and reviews are folded into their
  issue or pull request. No diffs: a pull request lists its changed files with
  line counts. The source declares ``cognee_document_source = "github"``.
* **Incremental sync**: per repository, an ``updated_at`` cursor on
  ``GET /issues?since=``, which returns issues and pull requests together.
  Every listing page is requested with its stored ETag, and a ``304`` costs no
  rate-limit quota, so an unchanged repository costs almost nothing.
* **Names an issue borrows**: renaming a milestone or a label does not move
  the ``updated_at`` of the issues that show it. A short fingerprint of each
  issue's header lines is kept in state, and an issue whose fingerprint
  changes is rendered again.
* **Forget-on-delete**: every run lists all issue numbers of each repository
  (the sweep). A number that was there before and is missing now is asked for
  once: ``410`` (deleted) or ``301`` (transferred) means forget it. Any other
  answer keeps the document and asks again next run, so a permissions problem
  can never delete anything and one odd issue cannot stall the sync.
* **Safety**: every listing is finished before the first row is yielded, and
  state is written only after the last one. A failed listing therefore deletes
  nothing and moves no cursor; the next run starts from the same point. An
  issue that fails on its own keeps its document and is retried next run; a
  ``401``, an exhausted rate limit or several failures in a row abort.
* **Several sources in one dataset**: ``resource_name`` names the dlt table and
  the sync state. Give each token or selection its own name.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlencode

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

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

# Retry budget for rate-limited / transient responses: five attempts with
# 1, 2, 4, 8 s back-off between them, about 15 s of outage tolerated per request.
_MAX_RETRIES = 5
# The 5xx answers GitHub's docs describe as temporary.
_TRANSIENT_STATUSES = (500, 502, 503, 504)
# Longest single wait for a rate limit to reset (GitHub's primary window is an hour).
_MAX_RATE_LIMIT_WAIT_SECONDS = 3600
# GitHub's advice for a secondary limit that says nothing about how long to wait.
_SECONDARY_LIMIT_WAIT_SECONDS = 60.0

# GitHub applies updated_at with a lag of several seconds (seen live: no move
# 3 s after a review, moved 15 s later), so the change listing starts well
# before the cursor and a fetched copy is only trusted as final once it is a
# minute older than its timestamp. Both are wide margins over the lag seen.
_CURSOR_OVERLAP = timedelta(minutes=5)
_SETTLE_MARGIN = timedelta(seconds=60)

# Abort when this many issues in a row fail on their own. One bad issue is kept
# and retried next run, but a streak means GitHub itself is failing, and every
# further issue would spend five retries (about 15 s) for nothing.
_MAX_FAILURES_IN_A_ROW = 3

# A pull request lists at most this many changed files: one page of /files at
# GitHub's maximum page size, so a huge pull request costs one request.
_MAX_FILES = 100


class GitHubAPIError(RuntimeError):
    """A non-retryable (or retries-exhausted) error response from the GitHub API."""

    def __init__(self, status: int, message: str, rate_limited: bool = False):
        super().__init__(f"GitHub API returned {status}: {message}")
        self.status = status
        # True when the retries ran out while GitHub was still rate limiting.
        self.rate_limited = rate_limited


class GitHubListingError(RuntimeError):
    """A listing that answered but cannot be followed to a complete end."""


class GitHubUnavailableError(RuntimeError):
    """Too many issues in a row failed: GitHub is failing, not one issue."""


class GitHubSyncStoppedError(RuntimeError):
    """The host's ``check_active`` raised an ``OSError``; the sync must stop.

    Network errors are ``OSError`` too and are retried, or kept per issue, so an
    ``OSError`` from the host's check is re-raised as this type, which nothing
    in the connector catches. Other exceptions from the check pass unchanged.
    """


# ---------------------------------------------------------------------------
# Auth / HTTP helpers
# ---------------------------------------------------------------------------
def _make_session(token: str) -> Any:
    """Build a ``requests`` session authenticated with a GitHub token."""
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - requests is a declared dependency
        raise ImportError(
            'The GitHub connector requires "requests". Install the package:\n'
            "    pip install cognee-community-connector-github"
        ) from exc

    session = requests.Session()
    session.headers.update({**_HEADERS, "Authorization": f"Bearer {token}"})
    return session


def _url(path: str, params: dict | None = None) -> str:
    """The absolute URL for an API path. ``Link`` URLs are already absolute."""
    url = path if path.startswith("https://") else f"{_API_BASE}{path}"
    if not url.startswith(_API_BASE + "/"):
        # The session carries the token: never send it anywhere else.
        raise ValueError(f"refusing to call a non-GitHub-API URL: {url}")
    return f"{url}?{urlencode(params)}" if params else url


def _get(
    session: Any,
    url: str,
    *,
    etag: str | None = None,
    accept: str | None = None,
    follow_redirects: bool = True,
    ok: tuple[int, ...] = (200,),
) -> Any:
    """GET an absolute API URL and return the response, retrying rate limits and outages.

    A ``304`` (only possible when ``etag`` is sent) and any status in ``ok``
    are returned. Rate limits are waited out (see ``_rate_limit_wait``), 5xx
    and network errors back off, and anything else raises
    :class:`GitHubAPIError`, including a ``403`` that is not a rate limit.
    """
    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    if accept:
        headers["Accept"] = accept
    for attempt in range(_MAX_RETRIES):
        last_attempt = attempt == _MAX_RETRIES - 1
        try:
            response = session.get(
                url, headers=headers, timeout=_TIMEOUT_SECONDS, allow_redirects=follow_redirects
            )
        except OSError as exc:
            # requests' connection and timeout errors all derive from OSError.
            if last_attempt:
                raise
            delay = float(2**attempt)
            logger.warning("GitHub: %s; retrying in %.1fs.", exc, delay)
            time.sleep(delay)
            continue

        status = response.status_code
        if status in ok or (etag and status == 304):
            return response
        wait = _rate_limit_wait(response)
        if wait is not None and not last_attempt:
            logger.warning("GitHub: rate limited on %s; waiting %.0fs.", url, wait)
            time.sleep(wait)
            continue
        if status in _TRANSIENT_STATUSES and not last_attempt:
            delay = float(2**attempt)
            logger.warning("GitHub: HTTP %d on %s; retrying in %.1fs.", status, url, delay)
            time.sleep(delay)
            continue
        raise GitHubAPIError(status, _error_message(response), rate_limited=wait is not None)

    raise AssertionError("unreachable: the loop returns or raises")  # pragma: no cover


def _rate_limit_wait(response: Any) -> float | None:
    """Seconds to wait if ``response`` is a rate limit, else ``None``.

    GitHub signals a rate limit with ``429``, or with ``403`` plus either
    ``retry-after`` (secondary limits) or ``x-ratelimit-remaining: 0`` (the
    primary limit). A secondary limit can also come as a bare ``403`` whose
    message says "secondary rate limit"; GitHub's advice is then to wait at
    least a minute. Any other ``403`` is a permission error and must not be
    retried. ``retry-after`` is honoured as given; otherwise the wait runs
    until ``x-ratelimit-reset``, measured against GitHub's own ``Date`` so a
    wrong local clock cannot shorten it.
    """
    headers = response.headers or {}
    retry_after = headers.get("Retry-After")
    exhausted = headers.get("X-RateLimit-Remaining") == "0"
    secondary = "secondary rate limit" in _error_message(response).lower()
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
    return min(max(reset - now.timestamp(), 0.0) + 1.0, float(_MAX_RATE_LIMIT_WAIT_SECONDS))


def _error_message(response: Any) -> str:
    """Extract GitHub's ``message``, falling back to the raw body."""
    try:
        return str(response.json()["message"])
    except (ValueError, KeyError, TypeError):
        return str(getattr(response, "text", ""))[:200]


def _json(response: Any, shape: type) -> Any:
    """The JSON body of a ``200``, which must be a ``list`` or a ``dict`` as expected.

    Anything else (not JSON, or the wrong shape) raises: an empty or odd body
    must never be read as an empty listing, which would look like deletions.
    """
    try:
        body = response.json()
    except ValueError as exc:
        raise GitHubListingError(f"GitHub answered {response.status_code} without JSON") from exc
    if not isinstance(body, shape):
        raise GitHubListingError(
            f"GitHub answered {response.status_code} with a {type(body).__name__} "
            f"where a {shape.__name__} was expected"
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
    """GitHub's clock when it answered, from the ``Date`` response header."""
    try:
        return parsedate_to_datetime((response.headers or {}).get("Date"))
    except (TypeError, ValueError):
        return None


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
    session: Any, first_url: str, previous: dict, key: Callable[[dict], list]
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
    ``Date``). The items are at least as fresh as that moment.
    """
    fresh: list[dict] = []
    reused: list[list] = []
    pages: dict[str, dict] = {}
    dates: list[datetime | None] = []
    url: str | None = first_url
    while url:
        if url in pages:  # a Link loop would otherwise never end
            raise GitHubListingError(f"GitHub's Link pagination loops back to {url}; stopping")
        stored = previous.get(url) or {}
        response = _get(session, url, etag=stored.get("etag"))
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


def _list_all(session: Any, url: str) -> tuple[list[dict], datetime | None]:
    """Every item of a small listing (comments, reviews), with GitHub's clock at its start."""
    items: list[dict] = []
    dates: list[datetime | None] = []
    next_url: str | None = url
    while next_url:
        response = _get(session, next_url)
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
# Per-repository listing (step 1 of sync_repos)
# ---------------------------------------------------------------------------
def _sweep_key(issue: dict) -> list:
    """What is kept per issue of a sweep page: its number and whether it is a PR."""
    return [issue["number"], "pull_request" in issue]


def _change_key(issue: dict) -> list:
    return [issue["number"], "pull_request" in issue, issue.get("updated_at") or ""]


def _fetch_repo(session: Any, repo: str, previous: dict, include_readme: bool) -> dict:
    """The repository object and README, or ``{"unchanged": True, ...}``.

    Both are asked for with their ETags. The repository's ETag changes on every
    push, and README text is not kept in state, so the README is fetched in
    full whenever the repository object changed. Only when both are unchanged
    (a repository without README stays without one) is the repository document
    left as it is. A selected repository that cannot be read stops the run.
    """
    try:
        response = _get(session, _url(f"/repos/{repo}"), etag=previous.get("repo_etag"))
    except GitHubAPIError as exc:
        if exc.status in (403, 404) and not exc.rate_limited:
            raise GitHubAPIError(
                exc.status, f"repository {repo} does not exist or the token cannot read it"
            ) from exc
        raise
    repo_same = response.status_code == 304

    readme_response = None
    # Switching include_readme off must take the README out of the document.
    readme_same = previous.get("readme_etag") is None
    if include_readme:
        # "" marks "no README last time"; None means it was never looked at.
        stored = previous.get("readme_etag")
        readme_response = _get(
            session,
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
        response = _get(session, _url(f"/repos/{repo}"))
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


class _ItemFailures:
    """The issues of one run that failed on their own and are kept for a retry.

    Only per-item failures are kept: a :class:`GitHubAPIError`, a
    :class:`GitHubListingError` or an ``OSError`` that outlasted the retries.
    A ``401`` or an exhausted rate limit would fail every later issue the same
    way, so it is re-raised, and so is the ``_MAX_FAILURES_IN_A_ROW``-th
    failure in a row. Anything else (``check_active``'s errors among them) is
    never caught in the first place.
    """

    def __init__(self) -> None:
        self.in_a_row = 0
        self.kept = 0

    def keep(self, exc: Exception, endpoint: str) -> None:
        """Count a per-item failure and log it, or raise if it is systemic."""
        if isinstance(exc, GitHubAPIError) and (exc.status == 401 or exc.rate_limited):
            raise exc
        self.in_a_row += 1
        if self.in_a_row >= _MAX_FAILURES_IN_A_ROW:
            raise GitHubUnavailableError(
                f"{self.in_a_row} issues in a row failed, the last on {endpoint}; stopping"
            ) from exc
        what = f"HTTP {exc.status}" if isinstance(exc, GitHubAPIError) else type(exc).__name__
        self.note_kept(endpoint, what)

    def note_kept(self, endpoint: str, why: str) -> None:
        self.kept += 1
        logger.warning("GitHub: kept %s for a retry next run (%s).", endpoint, why)

    def succeeded(self) -> None:
        self.in_a_row = 0


# What a per-item request can fail with once the retries are spent.
_ITEM_ERRORS = (GitHubAPIError, GitHubListingError, OSError)


def _confirm_gone(session: Any, repo: str, number: int, failures: _ItemFailures) -> bool:
    """True when a number missing from the sweep really is gone.

    Two answers allow forgetting it: ``410`` (deleted) and a ``301`` whose
    ``Location`` is an issue in *another* repository (transferred; seen live as
    ``/repos/<owner>/<other>/issues/<n>``). A renamed repository answers ``301``
    too, pointing back at itself, so any other ``Location`` does not count.
    Every other outcome, including ``404`` (which GitHub also gives when the
    token lost access), ``200``, and an error that outlasts the retries, keeps
    the document: the run goes on and the next run asks again. One odd issue
    must neither delete anything nor stop every run.
    """
    endpoint = f"/repos/{repo}/issues/{number}"
    try:
        response = _get(session, _url(endpoint), follow_redirects=False, ok=(301, 410, 200))
    except _ITEM_ERRORS as exc:
        failures.keep(exc, endpoint)
        return False
    failures.succeeded()
    if response.status_code == 410:
        return True
    if response.status_code == 301:
        target = _transfer_target((response.headers or {}).get("Location") or "")
        if target and target != repo:
            return True
    failures.note_kept(endpoint, f"missing from the listing, answering {response.status_code}")
    return False


def _transfer_target(location: str) -> str | None:
    """``owner/name`` (lower case) of an issue URL ``.../repos/<owner>/<name>/issues/<n>``."""
    path = location.removeprefix(_API_BASE).split("?")[0].strip("/").split("/")
    if len(path) == 5 and path[0] == "repos" and path[3] == "issues" and path[4].isdigit():
        return f"{path[1]}/{path[2]}".lower()
    return None


# ---------------------------------------------------------------------------
# Sync (pure given a session + state dict, so it is unit-testable)
# ---------------------------------------------------------------------------
def sync_repos(
    session: Any,
    state: dict,
    *,
    repos: list[str],
    include_commits: bool = False,
    include_bots: bool = False,
    include_readme: bool = True,
) -> Iterator[dict[str, Any]]:
    """Yield changed documents since the last run, plus hard-delete markers.

    ``state`` holds ``known_ids`` (every row id emitted so far) and, per
    repository, the ETags and page maps of its listings, the ``cursor`` (the
    newest issue ``updated_at`` seen), ``seen`` (issues inside the overlap whose
    rendered copy is final), ``fingerprints`` (issue number -> header hash) and,
    with ``include_commits``, the commit cursor and the commit shas emitted.

    The work is split so that a failure can never be mistaken for a deletion:

    1. *List.* For every repository: the repository and README, the sweep of
       all issue numbers, the change listing, the releases and the commits;
       then every number missing from the sweep is asked for, and kept unless
       GitHub says it is gone. A failed listing propagates before a single row
       is yielded.
    2. *Render.* Fetch and yield each changed issue and pull request once,
       then the repository, release and commit rows that changed. An issue
       that fails on its own keeps its old document and is retried next run.
    3. *Delete.* Yield a hard-delete marker for every known id that is no
       longer current.
    4. *Commit.* Only now write the new ids, cursors, ETags and fingerprints.
    """
    known_ids: set[str] = set(state.get("known_ids", []))
    previous_repos: dict = state.get("repos", {})
    failures = _ItemFailures()
    plans = [
        _plan_repo(
            session,
            repo,
            previous_repos.get(repo) or {},
            known_ids,
            include_commits,
            include_bots,
            include_readme,
            failures,
        )
        for repo in repos
    ]

    current_ids: set[str] = set()
    counts = {"issues": 0, "releases": 0, "commits": 0}
    for plan in plans:
        current_ids |= plan["current_ids"]
        for row in _render_repo_rows(session, plan, include_bots, failures):
            if row.pop("_kind") == "issue":
                counts["issues"] += 1
            yield row
        counts["releases"] += len(plan["fresh_releases"])
        counts["commits"] += len(plan["new_commits"])

    deleted = known_ids - current_ids
    for row_id in sorted(deleted):
        yield _deleted_row(row_id)

    state["known_ids"] = sorted(current_ids)
    state["repos"] = {plan["repo"]: plan["next_state"] for plan in plans}
    logger.info(
        "GitHub: %d issue(s)/PR(s) rendered, %d release(s), %d commit(s), %d deletion(s) "
        "across %d repo(s); kept %d item(s) for retry.",
        counts["issues"],
        counts["releases"],
        counts["commits"],
        len(deleted),
        len(repos),
        failures.kept,
    )


def _plan_repo(
    session: Any,
    repo: str,
    previous: dict,
    known_ids: set[str],
    include_commits: bool,
    include_bots: bool,
    include_readme: bool,
    failures: _ItemFailures,
) -> dict[str, Any]:
    """Step 1 for one repository: every listing and check, nothing yielded."""
    if previous.get("include_bots") != include_bots:
        # Stored fingerprints and pages were made with the other bot setting:
        # drop them, so every issue is compared (and rendered) afresh.
        previous = {k: v for k, v in previous.items() if k not in ("fingerprints", "sweep_pages")}
    fingerprints: dict[str, str] = previous.get("fingerprints", {})
    seen: dict[str, str] = previous.get("seen", {})
    cursor: str = previous.get("cursor", "")

    repo_info = _fetch_repo(session, repo, previous, include_readme)

    # The sweep: every issue and pull request that exists right now, oldest first.
    sweep_url = _url(
        f"/repos/{repo}/issues",
        {"state": "all", "sort": "created", "direction": "asc", "per_page": _PER_PAGE},
    )
    fresh, reused, sweep_pages, swept_at = _list_pages(
        session, sweep_url, previous.get("sweep_pages", {}), _sweep_key
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
            session, change_url, previous.get("change_pages", {}), _change_key
        )
        listed_at = _earliest([swept_at, changes_at])
        stamps.update({number: when for number, _, when in changed_reused})
        for issue in changed_fresh:
            stamps[issue["number"]] = issue.get("updated_at") or ""
            issues_by_number.setdefault(issue["number"], issue)
    else:
        stamps.update(
            {number: issue.get("updated_at") or "" for number, issue in issues_by_number.items()}
        )
        listed_at = swept_at

    settled = {number for number, when in stamps.items() if seen.get(str(number)) == when}
    changed = set(stamps) - settled
    # A header that would read differently (milestone or label renamed, bots
    # setting changed), or an issue with no fingerprint yet, is rendered too.
    changed |= {
        number for number in kinds if fingerprints.get(str(number)) != current_prints[str(number)]
    }
    changed &= set(kinds)

    # Numbers that were documents before and are not in the sweep any more.
    current_ids = {_issue_row_id(repo, number, is_pr) for number, is_pr in kinds.items()}
    missing = sorted(
        row_id
        for row_id in known_ids
        if row_id.startswith(("issue:", "pr:"))
        and _repo_of(row_id) == repo
        and row_id not in current_ids
    )
    for row_id in missing:
        if not _confirm_gone(session, repo, int(row_id.rsplit("#", 1)[1]), failures):
            current_ids.add(row_id)  # not confirmed gone: keep it, ask again next run

    # Releases: unchanged pages keep their rows, changed pages are re-emitted.
    fresh_releases, reused_releases, release_pages, _ = _list_pages(
        session,
        _url(f"/repos/{repo}/releases", {"per_page": _PER_PAGE}),
        previous.get("release_pages", {}),
        lambda release: [release["id"]],
    )
    release_ids = {f"release:{repo}@{rid}" for (rid,) in reused_releases}
    release_ids |= {f"release:{repo}@{release['id']}" for release in fresh_releases}

    # Commits (opt-in): append-only. GitHub lists them newest first; the
    # listing starts a little before the newest commit date seen and, like the
    # others, is asked for with its stored ETags.
    commit_shas: list[str] = list(previous.get("commit_shas", [])) if include_commits else []
    commit_cursor: str = previous.get("commit_cursor", "") if include_commits else ""
    commit_pages: dict = {}
    new_commits: list[dict] = []
    if include_commits and repo_info.get("default_branch"):
        params = {"sha": repo_info["default_branch"], "per_page": _PER_PAGE}
        if commit_cursor:
            params["since"] = _stamp(_parse(commit_cursor) - _CURSOR_OVERLAP)
        listed, reused_commits, commit_pages, _ = _list_pages(
            session,
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
    return {
        "repo": repo,
        "repo_info": repo_info,
        "issues": issues_by_number,
        "kinds": kinds,
        "changed": changed,
        "stamps": stamps,
        "settled": settled,
        "listed_at": listed_at,
        "fresh_releases": fresh_releases,
        "new_commits": new_commits,
        "current_ids": current_ids,
        "current_prints": current_prints,
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
            # Filled in by _render_repo_rows once the issues are fetched.
            "cursor": max([cursor, *stamps.values()]),
            "fingerprints": current_prints,
            "seen": {},
        },
    }


def _render_repo_rows(
    session: Any, plan: dict, include_bots: bool, failures: _ItemFailures
) -> Iterator[dict[str, Any]]:
    """Step 2 for one repository: fetch and yield what changed, then finish its state."""
    repo = plan["repo"]
    settled: set[int] = set(plan["settled"])
    for number in sorted(plan["changed"]):
        try:
            row, is_settled = _render_one(session, plan, number, include_bots)
        except _ITEM_ERRORS as exc:
            failures.keep(exc, f"/repos/{repo}/issues/{number}")
            # No row, so the old document stays; its id is in current_ids, so
            # it is not deleted. A fingerprint of "" matches nothing, so the
            # next run renders it again, and with no "seen" entry it is not
            # skipped either. An issue that was never emitted gets its first
            # document then; until that, its id in known_ids is harmless (a
            # hard delete of a row that does not exist is a no-op).
            plan["current_prints"][str(number)] = ""
            continue
        failures.succeeded()
        if is_settled:
            settled.add(number)
        yield row

    info = plan["repo_info"]
    if not info["unchanged"]:
        repo_obj = info["object"]
        yield {
            **_row(
                f"repo:{repo}",
                repo_obj.get("html_url") or "",
                repo_obj.get("full_name") or repo,
                _render_repo(repo_obj, info["readme"]),
            ),
            "_kind": "repo",
        }
    for release in plan["fresh_releases"]:
        yield {
            **_row(
                f"release:{repo}@{release['id']}",
                release.get("html_url") or "",
                release.get("name") or release.get("tag_name") or "",
                _render_release(release),
            ),
            "_kind": "release",
        }
    for commit in plan["new_commits"]:
        message = _text((commit.get("commit") or {}).get("message"))
        yield {
            **_row(
                f"commit:{repo}@{commit['sha']}",
                commit.get("html_url") or "",
                message.splitlines()[0] if message else commit["sha"][:7],
                _render_commit(commit),
            ),
            "_kind": "commit",
        }

    cursor = plan["next_state"]["cursor"]
    window_start = _stamp(_parse(cursor) - _CURSOR_OVERLAP) if cursor else ""
    plan["next_state"]["seen"] = {
        str(number): when
        for number, when in plan["stamps"].items()
        if number in settled and when >= window_start
    }


def _render_one(
    session: Any, plan: dict, number: int, include_bots: bool
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
        response = _get(session, _url(f"/repos/{repo}/issues/{number}"))
        issue, body_at = _json(response, dict), _server_time(response)
    is_pr = plan["kinds"][number]
    comments, fetched_at = _list_all(
        session, _url(f"/repos/{repo}/issues/{number}/comments", {"per_page": _PER_PAGE})
    )
    comments = _conversation(comments, include_bots)
    if is_pr:
        pull = _json(_get(session, _url(f"/repos/{repo}/pulls/{number}")), dict)
        reviews, _ = _list_all(
            session, _url(f"/repos/{repo}/pulls/{number}/reviews", {"per_page": _PER_PAGE})
        )
        review_comments, _ = _list_all(
            session, _url(f"/repos/{repo}/pulls/{number}/comments", {"per_page": _PER_PAGE})
        )
        # One page only: the total comes from changed_files, not from paging.
        files = _json(
            _get(session, _url(f"/repos/{repo}/pulls/{number}/files", {"per_page": _MAX_FILES})),
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
    return {**row, "_kind": "issue"}, settled


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def github_source(
    token: str | None = None,
    repos: list[str] | None = None,
    include_commits: bool = False,
    include_bots: bool = False,
    include_readme: bool = True,
    session: Any = None,
    *,
    resource_name: str = GITHUB_TABLE_NAME,
    check_active: Callable[[], None] | None = None,
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
        session: Pre-built ``requests`` session. Mainly an injection point for
            tests; when omitted one is built from the token.
        resource_name: Stable dlt resource (table) name, which also scopes the
            sync state. Give each token or repository selection its own name
            when several sync into one dataset; sharing a name would make each
            run forget the other's documents.
        check_active: Optional host authorization checkpoint. It is called
            before every GitHub request and around every yielded row, and
            stops the sync by raising; nothing is deleted and no state moves.

    Returns:
        A ``dlt`` resource (``resource_name``) configured with
        ``primary_key="id"``, ``write_disposition="merge"`` and an ``_deleted``
        hard-delete column. Hand it to ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:  # pragma: no cover - dlt is a declared dependency
        raise ImportError(
            'The GitHub connector requires "dlt". Install the package:\n'
            "    pip install cognee-community-connector-github"
        ) from exc

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError(
            "The GitHub connector requires a cognee build with table-scoped dlt document "
            "cleanup (cognee>=1.6). Upgrade cognee so upstream deletions are forgotten."
        )

    if isinstance(repos, str):
        repos = [repos]
    selected = list(dict.fromkeys(_repo_key(repo) for repo in repos or []))
    if not selected or any(repo.count("/") != 1 or not all(repo.split("/")) for repo in selected):
        raise ValueError('github_source requires repos=["owner/name", ...]')

    resolved_token = token or os.environ.get("GITHUB_TOKEN")
    if session is None and not resolved_token:
        raise ValueError("GitHub token required: pass token= or set GITHUB_TOKEN.")

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        # _deleted is a boolean hard-delete marker: rows where it is True are
        # removed from the dlt destination on merge, which propagates the
        # deletion through cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def github_documents():
        client = session or _make_session(resolved_token)
        check = _stop_on(check_active) if check_active is not None else None
        if check is not None:
            # The listing phase makes many requests before the first row.
            client = _GuardedSession(client, check)
        yield from dlt_utils.guarded_rows(
            sync_repos(
                client,
                dlt.current.resource_state(),
                repos=selected,
                include_commits=include_commits,
                include_bots=include_bots,
                include_readme=include_readme,
            ),
            check,
        )

    resource = github_documents()
    # Opt into the document ingestion path (row -> text document -> cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(resource, DOCUMENT_SOURCE_ATTR, GITHUB_SOURCE_NAME)
    # Give the incremental state its own dlt pipeline per dataset, so syncing
    # other dlt sources (or another dataset) cannot disturb cursors and ETags.
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    return resource


def _stop_on(check_active: Callable[[], None]) -> Callable[[], None]:
    """``check_active``, turning an ``OSError`` it raises into ``GitHubSyncStoppedError``."""

    def check() -> None:
        try:
            check_active()
        except OSError as exc:
            raise GitHubSyncStoppedError(f"check_active stopped the sync: {exc!r}") from exc

    return check


class _GuardedSession:
    """A session that asks the host's ``check_active`` before every request."""

    def __init__(self, session: Any, check_active: Callable[[], None]):
        self._session = session
        self._check_active = check_active

    def get(self, *args: Any, **kwargs: Any) -> Any:
        self._check_active()
        return self._session.get(*args, **kwargs)
