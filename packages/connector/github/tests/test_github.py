"""Unit tests for the GitHub connector.

The GitHub REST API is mocked by ``FakeGitHub``, an in-memory stand-in for a
``requests`` session, so no token and no network are needed. Its behaviour
mirrors what was observed against real repositories: ``GET /issues`` returns
issues and pull requests together and honours ``since``; pages chain through
``Link: rel="next"`` URLs with opaque cursors; a request with a matching
``If-None-Match`` answers ``304``; a page's ETag changes when anything on it
does; renaming a label or milestone leaves ``updated_at`` alone; a deleted
issue answers ``410`` and a transferred one ``301``; bots have
``user.type == "Bot"``; and every response carries a ``Date`` header.

Three layers:

* pure tests for rendering, the header fingerprint and the HTTP helpers;
* ``sync_repos`` driven with a plain dict as state (ingest, cursor, ETags,
  renames, deletion, and the failure paths that must not delete anything);
* a real ``dlt`` pipeline into a temp sqlite destination (edit and delete
  across runs, two sources in one dataset, ``check_active``), plus the
  document-source tag core routes on.
"""

import copy
import hashlib
import json
import random
from email.utils import format_datetime
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from uuid import NAMESPACE_OID, uuid5

import pytest
from cognee.tasks.ingestion.dlt_utils import document_source_tag, pipeline_name_for_source
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

from cognee_community_connector_github import github
from cognee_community_connector_github.github import (
    GITHUB_SOURCE_NAME,
    GitHubAPIError,
    GitHubListingError,
    _fingerprint,
    _get,
    _header_lines,
    _next_url,
    _parse,
    _rate_limit_wait,
    _render_issue,
    _render_pull,
    _render_repo,
    _url,
    github_source,
    sync_repos,
)

API = "https://api.github.com"
REPO = "acme/site"


def at(day, second=0):
    """A deterministic GitHub-style timestamp, ``at(1) < at(1, 5) < at(2)``."""
    return f"2024-03-{day:02d}T10:00:{second:02d}Z"


NOW = _parse("2024-03-28T00:00:00Z")  # what the fake's Date header says by default


def user(login, kind="User"):
    return {"login": login, "type": kind}


# ---------------------------------------------------------------------------
# Fake GitHub REST API
# ---------------------------------------------------------------------------
class _Resp:
    def __init__(self, status, payload=None, headers=None, text=None):
        self.status_code = status
        self.headers = headers or {}
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        return self._payload


def _error(status, message="Server Error", headers=None):
    return _Resp(status, {"message": message}, headers)


class FakeRepo:
    def __init__(self, full_name):
        self.object = {
            "full_name": full_name,
            "html_url": f"https://github.com/{full_name}",
            "description": "The marketing site.",
            "topics": ["web", "astro"],
            "homepage": "",
            "language": "TypeScript",
            "default_branch": "main",
            "archived": False,
        }
        self.readme = "# Site\n\nHow to build the site."
        self.issues = {}  # number -> issue dict (as GET /issues returns it)
        self.comments = {}  # number -> [comment]
        self.pulls = {}  # number -> extra PR fields (base/head/changed_files)
        self.reviews = {}  # number -> [review]
        self.review_comments = {}  # number -> [review comment]
        self.files = {}  # number -> [file]
        self.releases = []
        self.commits = []  # newest first, as GitHub lists them
        self.gone = {}  # number -> 410, or the Location of a 301


class FakeGitHub:
    """Minimal stand-in for a ``requests`` session talking to GitHub."""

    def __init__(self):
        self.repos = {}
        self.page_size = 100
        self.clock = NOW  # the Date header: a datetime, a string, None, or url -> one of those
        self.calls = []  # (url, headers) of every request
        self.statuses = []  # (url, status) of every request
        self._queued = []  # (predicate(url), response) answered once, in order

    # -- fixture builders ----------------------------------------------------
    def add_repo(self, full_name=REPO):
        self.repos[full_name.lower()] = FakeRepo(full_name)
        return self.repos[full_name.lower()]

    def add_issue(self, number, *, updated_at, repo=REPO, created_at=None, pr=False, **fields):
        issue = {
            "number": number,
            "title": f"Issue {number}",
            "body": "",
            "state": "open",
            "state_reason": None,
            "user": user("ada"),
            "assignees": [],
            "labels": [],
            "milestone": None,
            "html_url": f"https://github.com/{repo}/{'pull' if pr else 'issues'}/{number}",
            **fields,
            "created_at": created_at or updated_at,
            "updated_at": updated_at,
        }
        if pr:
            issue["pull_request"] = {"merged_at": None}
            issue.setdefault("draft", False)
            self.repos[repo.lower()].pulls[number] = {
                "base": {"ref": "main"},
                "head": {"ref": f"feature-{number}"},
                "changed_files": 0,
            }
        self.repos[repo.lower()].issues[number] = issue
        return issue

    def queue(self, response, when=lambda url: True):
        """Answer the next matching request with ``response`` instead of real data."""
        self._queued.append((when, response))

    def requested(self, fragment):
        return [url for url, _ in self.calls if fragment in url]

    def fresh_calls(self):
        """Requests that were not answered 304 (each costs rate-limit quota)."""
        return [url for url, status in self.statuses if status != 304]

    # -- requests.Session surface ----------------------------------------------
    def get(self, url, headers=None, timeout=None, allow_redirects=True):
        assert url.startswith(API + "/"), f"unexpected URL: {url}"
        assert timeout, "every request must carry a timeout"
        headers = headers or {}
        self.calls.append((url, headers))
        for index, (when, response) in enumerate(self._queued):
            if when(url):
                del self._queued[index]
                self.statuses.append((url, response.status_code))
                return response
        response = self._route(url, headers, allow_redirects)
        etag = response.headers.get("ETag")
        if etag and headers.get("If-None-Match") == etag:
            response = _Resp(304, None, {"ETag": etag}, text="")
        clock = self.clock(url) if callable(self.clock) else self.clock
        if clock is not None:
            date = clock if isinstance(clock, str) else format_datetime(clock, True)
            response.headers = {**response.headers, "Date": date}
        self.statuses.append((url, response.status_code))
        return response

    def _route(self, url, headers, allow_redirects):
        parsed = urlparse(url)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        parts = parsed.path.strip("/").split("/")
        assert parts[0] == "repos", f"unexpected path: {parsed.path}"
        repo = self.repos.get(f"{parts[1]}/{parts[2]}".lower())
        if repo is None:
            return _error(404, "Not Found")
        rest = parts[3:]
        if rest == []:
            return self._json(repo.object)
        if rest == ["readme"]:
            assert headers.get("Accept") == "application/vnd.github.raw"
            if repo.readme is None:
                return _error(404, "Not Found")
            return _Resp(200, None, {"ETag": _etag(repo.readme)}, text=repo.readme)
        if rest == ["issues"]:
            return self._issues(url, repo, query)
        if rest[0] == "issues" and len(rest) == 2:
            number = int(rest[1])
            if number in repo.gone:
                assert not allow_redirects, "a missing issue must be checked without redirects"
                if repo.gone[number] == 410:
                    return _error(410, "This issue was deleted")
                return _error(301, "Moved Permanently", {"Location": repo.gone[number]})
            if number not in repo.issues:
                return _error(404, "Not Found")
            return self._json(repo.issues[number])
        if rest[0] == "issues" and rest[2] == "comments":
            return self._page(url, repo.comments.get(int(rest[1]), []), query)
        if rest[0] == "pulls":
            number = int(rest[1])
            if len(rest) == 2:
                return self._json({**repo.issues[number], **repo.pulls[number]})
            if rest[2] == "reviews":
                return self._page(url, repo.reviews.get(number, []), query)
            if rest[2] == "comments":
                return self._page(url, repo.review_comments.get(number, []), query)
            if rest[2] == "files":
                return self._page(url, repo.files.get(number, []), query)
        if rest == ["releases"]:
            return self._page(url, repo.releases, query)
        if rest == ["commits"]:
            assert query.get("sha") == repo.object["default_branch"]
            since = query.get("since")
            listed = [
                c for c in repo.commits if not since or c["commit"]["committer"]["date"] >= since
            ]
            return self._page(url, listed, query)
        raise AssertionError(f"unexpected path: {parsed.path}")

    def _issues(self, url, repo, query):
        assert query["state"] == "all"
        items = list(repo.issues.values())
        if "since" in query:
            items = [i for i in items if i["updated_at"] >= query["since"]]  # inclusive
        key = "updated_at" if query.get("sort") == "updated" else "created_at"
        items.sort(key=lambda i: (i[key], i["number"]))
        return self._page(url, items, query)

    def _json(self, body):
        return _Resp(200, copy.deepcopy(body), {"ETag": _etag(body)})

    def _page(self, url, items, query):
        """Answer one page, with a Link to the next as an opaque cursor URL."""
        size = min(int(query.get("per_page", 30)), self.page_size)
        start = int(query.get("after", "0").removeprefix("c"))
        page = copy.deepcopy(items[start : start + size])
        headers = {"ETag": _etag(page)}
        if start + size < len(items):
            base = url.split("&after=")[0]
            headers["Link"] = f'<{base}&after=c{start + size}>; rel="next", <{base}>; rel="first"'
        return _Resp(200, page, headers)


def _etag(body):
    return '"' + hashlib.md5(json.dumps(body, sort_keys=True).encode()).hexdigest() + '"'


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    """Record retry waits instead of sleeping."""
    recorded = []
    monkeypatch.setattr(github.time, "sleep", recorded.append)
    return recorded


@pytest.fixture
def fake():
    session = FakeGitHub()
    session.statuses = []
    session.add_repo()
    return session


def run_sync(session, state, repos=(REPO,), **kwargs):
    session.calls.clear()
    session.statuses = []
    return list(sync_repos(session, state, repos=list(repos), **kwargs))


def doc_rows(rows, prefix=""):
    return {r["id"]: r for r in rows if not r["_deleted"] and r["id"].startswith(prefix)}


def tombstones(rows):
    return [r for r in rows if r["_deleted"]]


def fetched_issues(session):
    """Issue/PR numbers whose comments were fetched, i.e. that were rendered."""
    return sorted(
        {
            int(url.split("/issues/")[1].split("/")[0])
            for url in session.requested("/comments")
            if "/issues/" in url
        }
    )


def comment(login, body, kind="User"):
    return {"user": user(login, kind), "body": body}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
ISSUE = {
    "number": 7,
    "title": "Checkout button is hidden on mobile",
    "body": "Steps: open the cart on a phone.\n",
    "state": "closed",
    "state_reason": "not_planned",
    "user": user("ada"),
    "assignees": [user("lin"), user("grace")],
    "labels": [{"name": "ui"}, {"name": "bug"}],
    "milestone": {"title": "v2.0"},
    "updated_at": at(3),
    "comments": 2,
}


def test_render_issue_includes_header_body_and_comments():
    comments = [comment("grace", "Reproduced on iOS."), comment("ada", "  Fixed in #9. ")]
    assert _render_issue(ISSUE, comments) == (
        "Type: issue\n"
        "State: closed (not planned)\n"
        "Author: ada\n"
        "Assignees: grace, lin\n"
        "Labels: bug, ui\n"
        "Milestone: v2.0\n"
        "\n"
        "Steps: open the cart on a phone.\n"
        "\n"
        "Comments:\n"
        "- grace: Reproduced on iOS.\n"
        "- ada: Fixed in #9."
    )


def test_render_issue_minimal():
    issue = {"number": 1, "state": "open", "user": user("ada")}
    assert _render_issue(issue, []) == "Type: issue\nState: open\nAuthor: ada"


def test_render_issue_ignores_volatile_fields():
    noisy = {**ISSUE, "updated_at": at(9), "comments": 40, "reactions": {"+1": 3}}
    assert _render_issue(noisy, []) == _render_issue(ISSUE, [])


def test_render_pull_lists_reviews_and_files_without_patches():
    issue = {**ISSUE, "state": "closed", "state_reason": None, "pull_request": {"merged_at": at(4)}}
    pull = {"base": {"ref": "main"}, "head": {"ref": "fix-checkout"}, "changed_files": 2}
    reviews = [
        {"user": user("lin"), "state": "APPROVED", "body": ""},
        {"user": user("grace"), "state": "COMMENTED", "body": ""},  # only wraps line comments
        {"user": user("grace"), "state": "CHANGES_REQUESTED", "body": "Add a test."},
    ]
    review_comments = [{"user": user("grace"), "path": "src/cart.ts", "body": "Rename this."}]
    files = [
        {"filename": "src/cart.ts", "additions": 12, "deletions": 3, "patch": "@@ -1 +1 @@"},
        {"filename": "src/cart.test.ts", "additions": 40, "deletions": 0, "patch": "@@"},
    ]

    text = _render_pull(issue, pull, [], reviews, review_comments, files)

    assert text.startswith("Type: pull request\nState: merged\nAuthor: ada\n")
    assert "Branch: fix-checkout -> main" in text
    assert "Reviews:\n- lin (approved)\n- grace (changes requested): Add a test." in text
    assert "Review comments:\n- grace on src/cart.ts: Rename this." in text
    assert "Changed files:\n- src/cart.test.ts (+40/-0)\n- src/cart.ts (+12/-3)" in text
    assert "@@" not in text  # never a patch


def test_shuffled_lists_render_the_same_text():
    # The same pull request with its labels, assignees, comments, reviews,
    # review comments and files handed over in a different order.
    issue = {**ISSUE, "pull_request": {"merged_at": None}}
    comments = [{"id": i, "user": user(f"u{i}"), "body": f"comment {i}"} for i in range(5)]
    reviews = [
        {"id": 10 + i, "user": user(f"r{i}"), "state": "APPROVED", "body": f"ok {i}"}
        for i in range(3)
    ]
    review_comments = [
        {"id": 20 + i, "user": user("lin"), "path": f"f{i}.py", "body": f"nit {i}"}
        for i in range(3)
    ]
    files = [{"filename": f"src/{name}.py", "additions": 1, "deletions": 0} for name in "dbca"]
    pull = {"base": {"ref": "main"}, "head": {"ref": "fix"}, "changed_files": 4}
    expected = _render_pull(issue, pull, comments, reviews, review_comments, files)

    rng = random.Random(7)
    for _ in range(5):
        shuffled = {
            **issue,
            "labels": rng.sample(issue["labels"], len(issue["labels"])),
            "assignees": rng.sample(issue["assignees"], len(issue["assignees"])),
        }
        args = [rng.sample(x, len(x)) for x in (comments, reviews, review_comments, files)]
        assert _render_pull(shuffled, pull, *args) == expected
        assert _render_issue(shuffled, args[0]) == _render_issue(issue, comments)
    assert expected.index("comment 0") < expected.index("comment 4")  # oldest first


def test_render_pull_caps_the_file_list_and_says_how_many_more():
    issue = {"number": 3, "state": "open", "user": user("ada"), "pull_request": {}}
    files = [{"filename": f"f{i}.py", "additions": 1, "deletions": 0} for i in range(100)]

    text = _render_pull(issue, {"changed_files": 2340}, [], [], [], files)

    assert text.count("\n- f") == 100
    assert text.endswith("- f99.py (+1/-0)\n- ... and 2240 more files")


def test_render_repo_with_readme():
    repo = {
        "full_name": "acme/site",
        "description": "The marketing site.",
        "topics": ["web", "astro"],
        "homepage": "https://acme.dev",
        "language": "TypeScript",
        "default_branch": "main",
        "archived": True,
        "pushed_at": at(5),
        "stargazers_count": 12,
    }
    assert _render_repo(repo, "# Site\n") == (
        "Description: The marketing site.\nTopics: astro, web\nHomepage: https://acme.dev\n"
        "Language: TypeScript\nDefault branch: main\nArchived: yes\n\nREADME:\n# Site"
    )


# ---------------------------------------------------------------------------
# Header fingerprint
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "change",
    [
        {"state": "open"},
        {"state_reason": "completed"},
        {"user": user("eve")},
        {"assignees": [user("lin")]},
        {"labels": [{"name": "ui"}]},
        {"labels": [{"name": "ui"}, {"name": "defect"}]},  # a label renamed
        {"milestone": {"title": "v2.1"}},  # a milestone renamed
        {"milestone": None},
        {"draft": True},
        {"pull_request": {"merged_at": None}},
    ],
)
def test_fingerprint_changes_with_the_rendered_header(change):
    after = {**ISSUE, **change}
    assert _header_lines(after) != _header_lines(ISSUE)
    assert _fingerprint(_header_lines(after), False) != _fingerprint(_header_lines(ISSUE), False)
    assert _render_issue(after, []).startswith("\n".join(_header_lines(after)))


@pytest.mark.parametrize(
    "change",
    [
        {"title": "Another title"},
        {"body": "Rewritten."},
        {"comments": 99},
        {"updated_at": at(9)},
        {"assignees": [user("grace"), user("lin")]},  # same people, other order
        {"labels": [{"name": "bug"}, {"name": "ui"}]},
    ],
)
def test_fingerprint_ignores_everything_outside_the_header(change):
    after = {**ISSUE, **change}
    assert _fingerprint(_header_lines(after), False) == _fingerprint(_header_lines(ISSUE), False)


def test_fingerprint_depends_on_the_bot_setting():
    lines = _header_lines(ISSUE)
    assert _fingerprint(lines, False) != _fingerprint(lines, True)
    assert _fingerprint(["Type: issue", "State: open"], False) == "526cb66190"  # pinned


# ---------------------------------------------------------------------------
# HTTP helpers: URLs, Link pagination, rate limits
# ---------------------------------------------------------------------------
def test_url_refuses_anything_but_the_github_api():
    assert _url("/repos/a/b/issues", {"per_page": 100}) == f"{API}/repos/a/b/issues?per_page=100"
    with pytest.raises(ValueError, match="non-GitHub-API"):
        _url("https://evil.example/repos/a/b")


def test_next_url_is_taken_from_the_link_header_exactly():
    link = (
        "<https://api.github.com/repositories/1/issues?per_page=2&after=Y3Vy%3D&page=2>; "
        'rel="next", '
        "<https://api.github.com/repositories/1/issues?per_page=2&page=1&before=Y3Vy>; "
        'rel="prev"'
    )
    response = _Resp(200, [], {"Link": link})
    assert (
        _next_url(response)
        == "https://api.github.com/repositories/1/issues?per_page=2&after=Y3Vy%3D&page=2"
    )
    assert _next_url(_Resp(200, [], {})) is None


def test_listing_follows_link_urls_and_nothing_else(fake):
    fake.page_size = 2
    for number in range(1, 6):
        fake.add_issue(number, updated_at=at(number))

    run_sync(fake, {})

    sweep = [url for url in fake.requested("/issues?") if "sort=created" in url]
    assert len(sweep) == 3
    assert "after=" not in sweep[0]
    assert sweep[1].endswith("&after=c2") and sweep[2].endswith("&after=c4")


@pytest.mark.parametrize(
    "status, headers, expected",
    [
        (429, {"Retry-After": "7"}, 7.0),
        (403, {"Retry-After": "30"}, 30.0),  # secondary limit
        (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1711584120"}, 121.0),
        (403, {"X-RateLimit-Remaining": "4974"}, None),  # a permission error
        (403, {}, None),
        (404, {"Retry-After": "7"}, None),
    ],
)
def test_rate_limit_is_told_apart_from_a_permission_error(status, headers, expected):
    # Date is 2024-03-28T00:00:00Z = 1711584000; the reset is two minutes later.
    response = _Resp(status, {}, {**headers, "Date": format_datetime(NOW, True)})
    assert _rate_limit_wait(response) == expected


@pytest.mark.parametrize(
    "body",
    [
        _Resp(200, {"message": "not a list"}),  # a dict where a listing is expected
        _Resp(200, None, text="<html>maintenance</html>"),  # not JSON
    ],
)
def test_listing_with_a_body_of_the_wrong_shape_raises(seeded, body):
    state = {}
    run_sync(seeded, state)
    before = copy.deepcopy(state)
    del seeded.repos[REPO].issues[1]
    seeded.repos[REPO].gone[1] = 410
    if body.text.startswith("<"):
        body.json = lambda: json.loads(body.text)  # raises ValueError, like requests
    seeded.queue(body, when=lambda url: "sort=created" in url)

    with pytest.raises(GitHubListingError):
        run_sync(seeded, state)
    assert state == before  # never read as an empty listing, nothing deleted


def test_rate_limit_waits_then_succeeds_and_permission_error_raises_at_once(fake, sleeps):
    fake.queue(
        _error(
            403,
            "API rate limit exceeded",
            {
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": "1711584010",
                "Date": format_datetime(NOW, True),
            },
        )
    )
    assert _get(fake, _url(f"/repos/{REPO}")).status_code == 200
    assert sleeps == [11.0]

    fake.queue(
        _error(
            403,
            "Resource not accessible by personal access token",
            {"X-RateLimit-Remaining": "4974"},
        )
    )
    with pytest.raises(GitHubAPIError, match="not accessible") as excinfo:
        _get(fake, _url(f"/repos/{REPO}"))
    assert excinfo.value.status == 403
    assert sleeps == [11.0]  # not retried


def test_secondary_rate_limit_without_headers_waits_a_minute(fake, sleeps):
    message = "You have exceeded a secondary rate limit. Please wait a few minutes."
    fake.queue(_error(403, message, {"X-RateLimit-Remaining": "4000"}))

    assert _get(fake, _url(f"/repos/{REPO}")).status_code == 200
    assert sleeps == [60.0]


def test_rate_limit_that_outlasts_the_retries_is_not_reported_as_missing_access(fake):
    for _ in range(github._MAX_RETRIES):
        fake.queue(_error(403, "API rate limit exceeded", {"Retry-After": "1"}))

    with pytest.raises(GitHubAPIError, match="API rate limit exceeded") as excinfo:
        run_sync(fake, {})
    assert excinfo.value.rate_limited


def test_server_and_network_errors_back_off_then_succeed(sleeps):
    class Flaky(FakeGitHub):
        failures = 1

        def get(self, url, **kwargs):
            if self.failures:
                self.failures -= 1
                raise ConnectionError("connection reset")  # an OSError, like requests'
            return super().get(url, **kwargs)

    session = Flaky()
    session.statuses = []
    session.add_repo()
    session.queue(_error(502, "Bad Gateway"))
    assert _get(session, _url(f"/repos/{REPO}")).status_code == 200
    assert sleeps == [1.0, 2.0]


def test_retries_are_capped(fake, sleeps):
    for _ in range(github._MAX_RETRIES):
        fake.queue(_error(500))
    with pytest.raises(GitHubAPIError, match="500"):
        _get(fake, _url(f"/repos/{REPO}"))
    assert len(sleeps) == github._MAX_RETRIES - 1


# ---------------------------------------------------------------------------
# sync_repos: ingest, cursor, ETags
# ---------------------------------------------------------------------------
@pytest.fixture
def seeded(fake):
    """One issue with comments, one PR with a review and a file, one release."""
    repo = fake.repos[REPO]
    fake.add_issue(
        1,
        updated_at=at(2),
        title="Checkout button hidden",
        body="On phones.",
        labels=[{"name": "bug"}],
    )
    repo.comments[1] = [
        comment("grace", "Reproduced."),
        comment("github-actions[bot]", "Stale?", "Bot"),
    ]
    fake.add_issue(2, updated_at=at(3), pr=True, title="Fix the checkout button", body="Closes #1.")
    repo.reviews[2] = [
        {
            "user": user("copilot-pull-request-reviewer[bot]", "Bot"),
            "state": "COMMENTED",
            "body": "Summary of changes...",
        },
        {"user": user("lin"), "state": "APPROVED", "body": ""},
    ]
    repo.files[2] = [{"filename": "src/cart.ts", "additions": 3, "deletions": 1, "patch": "@@"}]
    repo.pulls[2]["changed_files"] = 1
    repo.releases = [
        {
            "id": 501,
            "tag_name": "v1.0.0",
            "name": "First release",
            "body": "Initial launch.",
            "prerelease": False,
            "draft": False,
            "html_url": "https://github.com/acme/site/releases/v1.0.0",
        }
    ]
    return fake


def test_first_sync_renders_repo_issues_pulls_and_releases(seeded):
    state = {}
    rows = run_sync(seeded, state)

    assert [r["id"] for r in rows] == [
        "issue:acme/site#1",
        "pr:acme/site#2",
        "repo:acme/site",
        "release:acme/site@501",
    ]
    issue, pull, repo, release = rows
    assert issue["url"] == "https://github.com/acme/site/issues/1"
    assert issue["title"] == "Checkout button hidden"
    assert issue["content"].endswith("Comments:\n- grace: Reproduced.")  # the bot is left out
    assert (
        "Reviews:\n- lin (approved)" in pull["content"]
        and "Summary of changes" not in pull["content"]
    )
    assert "Changed files:\n- src/cart.ts (+3/-1)" in pull["content"]
    assert repo["title"] == "acme/site" and repo["content"].endswith(
        "README:\n# Site\n\nHow to build the site."
    )
    assert (
        release["title"] == "First release"
        and release["content"] == "Tag: v1.0.0\n\nInitial launch."
    )
    assert state["known_ids"] == sorted(r["id"] for r in rows)
    assert state["repos"][REPO]["cursor"] == at(3)


def test_quiet_run_fetches_nothing_and_costs_no_quota(seeded):
    state = {}
    run_sync(seeded, state)

    # The first incremental run asks for the change listing for the first time
    # (a first sync uses the sweep instead), so that one request has no ETag yet.
    assert run_sync(seeded, state) == []
    assert fetched_issues(seeded) == []
    assert seeded.fresh_calls() == seeded.requested("since=")
    before = copy.deepcopy(state)

    rows = run_sync(seeded, state)

    assert rows == []
    assert seeded.fresh_calls() == []  # every request answered 304
    assert state == before


def test_edited_comment_rerenders_through_since(seeded):
    state = {}
    run_sync(seeded, state)

    seeded.repos[REPO].comments[1][0]["body"] = "Reproduced on iOS 17."
    seeded.repos[REPO].issues[1]["updated_at"] = at(9)  # verified live: an edit moves it
    rows = run_sync(seeded, state)

    assert fetched_issues(seeded) == [1]
    assert "Reproduced on iOS 17." in doc_rows(rows)["issue:acme/site#1"]["content"]
    since = seeded.requested("since=")
    assert since and "since=2024-03-03T09%3A55%3A00Z" in since[0]  # cursor minus five minutes
    assert state["repos"][REPO]["cursor"] == at(9)


def test_issue_fetched_soon_after_its_stamp_is_fetched_once_more(seeded):
    seeded.clock = _parse(at(3, 30))  # 30 s after PR 2's timestamp: not settled yet
    state = {}
    run_sync(seeded, state)
    assert "2" not in state["repos"][REPO]["seen"]

    seeded.clock = NOW
    run_sync(seeded, state)
    assert fetched_issues(seeded) == [2]  # once more, now well after its timestamp
    run_sync(seeded, state)
    assert fetched_issues(seeded) == []


def test_change_inside_the_same_second_is_picked_up(seeded):
    seeded.clock = _parse(at(3, 10))
    state = {}
    run_sync(seeded, state)

    # A second edit lands in the same second: updated_at does not move.
    seeded.repos[REPO].issues[2]["body"] = "Closes #1. Also fixes the footer."
    seeded.clock = NOW
    rows = run_sync(seeded, state)

    assert "Also fixes the footer." in doc_rows(rows)["pr:acme/site#2"]["content"]


def test_settled_by_the_listing_time_not_the_later_comment_fetch(seeded):
    # The body comes from the listing, read 30 s after PR 2's timestamp; the
    # comments are read two minutes later. The document is as old as its
    # oldest part, so it is not settled yet.
    def clock(url):
        return _parse(at(3, 30)) if "/issues?" in url else NOW

    seeded.clock = clock
    state = {}
    run_sync(seeded, state)
    assert "2" not in state["repos"][REPO]["seen"]

    seeded.clock = NOW
    run_sync(seeded, state)
    assert fetched_issues(seeded) == [2]  # fetched once more, then trusted
    assert state["repos"][REPO]["seen"] == {"2": at(3)}


def test_missing_date_header_means_not_settled(seeded):
    seeded.clock = None
    state = {}
    run_sync(seeded, state)
    assert state["repos"][REPO]["seen"] == {}


@pytest.mark.parametrize(
    "rename",
    [
        {"labels": [{"name": "defect"}]},  # label renamed: updated_at stays (verified live)
        {"milestone": {"title": "v1.0"}},  # milestone renamed: same
        {"assignees": [user("lin-renamed")]},
    ],
)
def test_renamed_label_or_milestone_rerenders_through_the_fingerprint(seeded, rename):
    state = {}
    run_sync(seeded, state)

    seeded.repos[REPO].issues[1].update(rename)  # no updated_at change
    rows = run_sync(seeded, state)

    assert fetched_issues(seeded) == [1]
    assert set(doc_rows(rows)) == {"issue:acme/site#1"}


def test_new_issue_with_an_old_timestamp_is_ingested(seeded):
    state = {}
    run_sync(seeded, state)

    seeded.add_issue(3, updated_at=at(1), created_at=at(1))  # e.g. transferred in
    rows = run_sync(seeded, state)

    assert "issue:acme/site#3" in doc_rows(rows)


def test_bots_are_dropped_by_default_and_toggling_rerenders(seeded):
    state = {}
    run_sync(seeded, state)

    rows = run_sync(seeded, state, include_bots=True)

    assert set(doc_rows(rows, "issue:")) | set(doc_rows(rows, "pr:")) == {
        "issue:acme/site#1",
        "pr:acme/site#2",
    }
    assert "- github-actions[bot]: Stale?" in doc_rows(rows)["issue:acme/site#1"]["content"]
    assert (
        "copilot-pull-request-reviewer[bot] (commented): Summary"
        in doc_rows(rows)["pr:acme/site#2"]["content"]
    )


def test_pr_with_many_files_fetches_one_page_of_files(fake):
    fake.add_issue(4, updated_at=at(1), pr=True)
    fake.repos[REPO].files[4] = [
        {"filename": f"f{i}.py", "additions": 1, "deletions": 0} for i in range(250)
    ]
    fake.repos[REPO].pulls[4]["changed_files"] = 250

    rows = run_sync(fake, {})

    assert len(fake.requested("/pulls/4/files")) == 1
    assert doc_rows(rows)["pr:acme/site#4"]["content"].endswith("- ... and 150 more files")


def test_commits_are_opt_in_and_appended_once(fake):
    def commit(sha, day, message):
        return {
            "sha": sha,
            "html_url": f"https://github.com/acme/site/commit/{sha}",
            "author": {"login": "ada"},
            "commit": {
                "message": message,
                "author": {"name": "Ada"},
                "committer": {"date": at(day)},
            },
        }

    fake.repos[REPO].commits = [
        commit("b" * 40, 2, "Fix footer\n\nLonger text."),
        commit("a" * 40, 1, "Initial commit"),
    ]

    assert not doc_rows(run_sync(fake, {}), "commit:")
    assert fake.requested("/commits") == []

    state = {}
    rows = doc_rows(run_sync(fake, state, include_commits=True), "commit:")
    assert list(rows) == [f"commit:acme/site@{'a' * 40}", f"commit:acme/site@{'b' * 40}"]
    assert (
        rows[f"commit:acme/site@{'b' * 40}"]["content"]
        == "Commit: bbbbbbb\nAuthor: Ada\n\nFix footer\n\nLonger text."
    )

    fake.repos[REPO].commits.insert(0, commit("c" * 40, 3, "Add pricing page"))
    rows = run_sync(fake, state, include_commits=True)
    assert list(doc_rows(rows, "commit:")) == [f"commit:acme/site@{'c' * 40}"]
    assert tombstones(rows) == []  # earlier commits stay current
    assert "since=2024-03-02T09%3A55%3A00Z" in fake.requested("/commits")[0]

    # Quiet: the commit listing is answered 304 like every other listing.
    run_sync(fake, state, include_commits=True)
    assert run_sync(fake, state, include_commits=True) == []
    assert fake.fresh_calls() == []


# ---------------------------------------------------------------------------
# sync_repos: the repository document and its README
# ---------------------------------------------------------------------------
def test_repo_changed_fetches_the_readme_without_its_etag(seeded):
    state = {}
    run_sync(seeded, state)

    seeded.repos[REPO].object["description"] = "The marketing site, v2."  # e.g. a push
    rows = run_sync(seeded, state)

    readme_calls = [h for url, h in seeded.calls if url.endswith("/readme")]
    assert "If-None-Match" not in readme_calls[0]
    assert doc_rows(rows)["repo:acme/site"]["content"].startswith(
        "Description: The marketing site, v2."
    )


def test_readme_changed_alone_rebuilds_the_repo_document(seeded):
    state = {}
    run_sync(seeded, state)

    seeded.repos[REPO].readme = "# Site\n\nNow with a deploy guide."
    rows = run_sync(seeded, state)

    assert doc_rows(rows)["repo:acme/site"]["content"].endswith("Now with a deploy guide.")


def test_repo_without_readme_has_no_section_and_stays_quiet(fake):
    fake.repos[REPO].readme = None
    state = {}
    rows = run_sync(fake, state)
    assert "README" not in doc_rows(rows)["repo:acme/site"]["content"]

    assert run_sync(fake, state) == []


def test_switching_include_readme_off_removes_the_readme(fake):
    state = {}
    run_sync(fake, state)

    rows = run_sync(fake, state, include_readme=False)

    assert "README" not in doc_rows(rows)["repo:acme/site"]["content"]
    assert run_sync(fake, state, include_readme=False) == []  # and quiet afterwards


def test_renamed_repository_keeps_syncing(fake, caplog):
    state = {}
    run_sync(fake, state)

    fake.repos[REPO].object["full_name"] = "acme/site-v2"  # GitHub redirects the old name
    rows = run_sync(fake, state)

    assert doc_rows(rows)["repo:acme/site"]["title"] == "acme/site-v2"


def test_include_readme_false_never_asks_for_it(fake):
    run_sync(fake, {}, include_readme=False)
    assert fake.requested("/readme") == []


def test_selected_repo_that_cannot_be_read_aborts_with_a_clear_message(fake):
    with pytest.raises(
        GitHubAPIError, match="acme/missing does not exist or the token cannot read it"
    ):
        run_sync(fake, {}, repos=["acme/missing"])


# ---------------------------------------------------------------------------
# sync_repos: deletion
# ---------------------------------------------------------------------------
TRANSFERRED = "https://api.github.com/repos/acme/other-repo/issues/12"  # shape seen live


@pytest.mark.parametrize("answer", [410, TRANSFERRED])  # deleted, transferred
def test_issue_gone_for_good_is_forgotten(seeded, answer):
    state = {}
    run_sync(seeded, state)

    del seeded.repos[REPO].issues[1]
    seeded.repos[REPO].gone[1] = answer
    rows = run_sync(seeded, state)

    assert tombstones(rows) == [{"id": "issue:acme/site#1", "_deleted": True}]
    assert "issue:acme/site#1" not in state["known_ids"]


@pytest.mark.parametrize(
    "answer",
    [
        "404",
        "500",
        "200",
        # A renamed repository answers 301 too, pointing back at itself.
        "https://api.github.com/repos/ACME/Site/issues/1",
        "https://api.github.com/repositories/4242/issues/1",
        "https://api.github.com/repos/acme/site-renamed",
    ],
)
def test_missing_number_with_any_other_answer_is_kept_and_asked_again(seeded, answer, caplog):
    state = {}
    run_sync(seeded, state)

    issue = seeded.repos[REPO].issues.pop(1)
    if answer.startswith("https://"):
        seeded.repos[REPO].gone[1] = answer
    elif answer == "500":
        for _ in range(github._MAX_RETRIES):
            seeded.queue(_error(500), when=lambda url: url.endswith("/issues/1"))
    elif answer == "200":
        seeded.queue(_Resp(200, issue), when=lambda url: url.endswith("/issues/1"))
    seeded.add_issue(3, updated_at=at(9))  # the rest of the sync still happens

    rows = run_sync(seeded, state)

    assert tombstones(rows) == []
    assert "issue:acme/site#3" in doc_rows(rows)
    assert "issue:acme/site#1" in state["known_ids"]
    # The next run asks again, and forgets it once GitHub says it is gone.
    seeded.repos[REPO].gone[1] = 410
    rows = run_sync(seeded, state)
    assert seeded.requested("/issues/1") == [f"{API}/repos/{REPO}/issues/1"]
    assert tombstones(rows) == [{"id": "issue:acme/site#1", "_deleted": True}]


@pytest.mark.parametrize("failing", ["sweep page 2", "change listing", "releases", "readme"])
def test_failed_listing_deletes_nothing_and_keeps_state(seeded, failing):
    seeded.page_size = 1  # the sweep spans two pages
    state = {}
    run_sync(seeded, state)
    before = copy.deepcopy(state)

    del seeded.repos[REPO].issues[1]
    seeded.repos[REPO].gone[1] = 410  # a real deletion is pending; it must wait
    seeded.repos[REPO].issues[2]["updated_at"] = at(9)
    match = {
        "sweep page 2": lambda url: "sort=created" in url and "after=" in url,
        "change listing": lambda url: "since=" in url,
        "releases": lambda url: "/releases" in url,
        "readme": lambda url: url.endswith("/readme"),
    }[failing]
    seeded.queue(_error(403, "Resource not accessible by personal access token"), when=match)
    if failing == "sweep page 2":
        seeded.add_issue(3, updated_at=at(9))  # keeps the sweep at two pages after the deletion

    rows = []
    with pytest.raises(GitHubAPIError):
        for row in sync_repos(seeded, state, repos=[REPO]):
            rows.append(row)

    assert rows == []
    assert state == before

    rows = run_sync(seeded, state)
    assert {"id": "issue:acme/site#1", "_deleted": True} in rows


def test_repo_removed_from_the_selection_is_forgotten(seeded):
    seeded.add_repo("acme/blog")
    seeded.add_issue(1, updated_at=at(1), repo="acme/blog")
    state = {}
    run_sync(seeded, state, repos=[REPO, "acme/blog"])
    assert "issue:acme/blog#1" in state["known_ids"]

    rows = run_sync(seeded, state, repos=[REPO])

    assert {r["id"] for r in tombstones(rows)} == {"issue:acme/blog#1", "repo:acme/blog"}
    assert "acme/blog" not in state["repos"]


def test_repo_names_are_case_insensitive(seeded):
    resource = github_source(session=seeded, repos=["Acme/Site", "acme/site"])
    rows = list(resource)
    assert {r["id"] for r in rows} >= {"repo:acme/site", "issue:acme/site#1"}
    assert len([r for r in rows if r["id"] == "repo:acme/site"]) == 1


# ---------------------------------------------------------------------------
# github_source: validation and dlt wiring
# ---------------------------------------------------------------------------
def test_github_source_resource_is_configured_for_merge_and_hard_delete(fake):
    resource = github_source(session=fake, repos=[REPO])
    assert resource.name == "github_documents"

    schema = resource.compute_table_schema()
    write_disposition = schema.get("write_disposition")
    if isinstance(write_disposition, dict):
        write_disposition = write_disposition.get("disposition")
    assert write_disposition == "merge"
    assert schema["columns"]["id"].get("primary_key") is True
    assert schema["columns"]["_deleted"].get("hard_delete") is True


def test_github_source_declares_document_marker_and_pipeline_scope(fake):
    source = github_source(session=fake, repos=[REPO])
    assert GITHUB_SOURCE_NAME == "github"
    assert document_source_tag(source) == "github"
    assert pipeline_name_for_source(source, "github") != "ingest_dlt_source"


def test_resource_name_names_the_table_and_scopes_the_state(fake):
    default = github_source(session=fake, repos=[REPO])
    work = github_source(session=fake, repos=[REPO], resource_name="github_work")

    assert work.name == "github_work"
    assert pipeline_name_for_source(work, "github") != pipeline_name_for_source(default, "github")


def test_build_document_data_item_tags_source():
    row = SimpleNamespace(
        row_data={
            "id": "issue:acme/site#1",
            "url": "https://github.com/acme/site/issues/1",
            "title": "Checkout button hidden",
            "content": "Type: issue",
        },
        content_hash="abc123",
        table_name="github_documents",
    )
    item = _build_document_data_item(row, uuid5(NAMESPACE_OID, "issue:acme/site#1"), "github")

    assert item.system_metadata["source"] == "github"
    assert item.system_metadata["external_id"] == "issue:acme/site#1"
    assert item.data == "# Checkout button hidden\n\nType: issue"


@pytest.mark.parametrize("repos", [None, [], ["acme"], ["acme/site/extra"], ["/site"]])
def test_github_source_requires_owner_name_repos(fake, repos):
    with pytest.raises(ValueError, match="owner/name"):
        github_source(session=fake, repos=repos)


def test_github_source_requires_a_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(ValueError, match="GITHUB_TOKEN"):
        github_source(repos=[REPO])


def test_github_source_reads_token_from_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    assert github_source(repos=[REPO]).name == "github_documents"


# ---------------------------------------------------------------------------
# dlt pipeline: a real merge into a temp sqlite destination
# ---------------------------------------------------------------------------
def _pipeline(tmp_path, name):
    import dlt

    return dlt.pipeline(
        pipeline_name=name,
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{(tmp_path / 'github.db').as_posix()}"),
        dataset_name="github_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def _run_pipeline(tmp_path, session, repos=(REPO,), **kwargs):
    session.calls.clear()
    session.statuses = []
    source = github_source(session=session, repos=list(repos), **kwargs)
    # One dlt pipeline per source scope, the way cognee names them.
    pipeline = _pipeline(tmp_path, pipeline_name_for_source(source, "github_ds"))
    pipeline.run(source)
    return pipeline


def _read_documents(pipeline, table="github_documents"):
    with (
        pipeline.sql_client() as client,
        client.execute_query(f"SELECT id, content FROM {table}") as cursor,
    ):
        return dict(cursor.fetchall())


def test_pipeline_deletion_removes_the_row_from_the_destination(seeded, tmp_path):
    # Goes red if the _deleted marker is not emitted or hard_delete is off:
    # the row must be gone, not kept emptied or with _deleted set.
    pipeline = _run_pipeline(tmp_path, seeded)
    assert "issue:acme/site#1" in _read_documents(pipeline)

    del seeded.repos[REPO].issues[1]
    seeded.repos[REPO].gone[1] = 410
    pipeline = _run_pipeline(tmp_path, seeded)

    with (
        pipeline.sql_client() as client,
        client.execute_query(
            "SELECT COUNT(*) FROM github_documents WHERE id = 'issue:acme/site#1'"
        ) as cursor,
    ):
        assert cursor.fetchone()[0] == 0
    assert "pr:acme/site#2" in _read_documents(pipeline)


def test_pipeline_two_sources_in_one_dataset_stay_apart(seeded, tmp_path):
    seeded.add_repo("acme/blog")
    seeded.add_issue(1, updated_at=at(1), repo="acme/blog")
    site = {"repos": [REPO], "resource_name": "github_site"}
    blog = {"repos": ["acme/blog"], "resource_name": "github_blog"}

    _run_pipeline(tmp_path, seeded, **site)
    _run_pipeline(tmp_path, seeded, **blog)
    site_docs = _read_documents(_run_pipeline(tmp_path, seeded, **site), "github_site")
    blog_docs = _read_documents(_run_pipeline(tmp_path, seeded, **blog), "github_blog")

    assert "issue:acme/site#1" in site_docs and "issue:acme/blog#1" not in site_docs
    assert set(blog_docs) == {"repo:acme/blog", "issue:acme/blog#1"}


def test_check_active_is_asked_before_every_request(seeded):
    checks = []
    source = github_source(
        session=seeded, repos=[REPO], check_active=lambda: checks.append(len(seeded.calls))
    )
    rows = list(source)

    assert rows
    # Asked before each request (and around each row), so never behind it.
    assert len(checks) > len(seeded.calls)
    assert sorted(set(checks)) == list(range(len(seeded.calls) + 1))


@pytest.mark.parametrize("stop_after_requests", [1, 6, 9])
def test_check_active_raising_stops_with_nothing_deleted_or_loaded(
    seeded, tmp_path, stop_after_requests
):
    _run_pipeline(tmp_path, seeded)
    before = _read_documents(_pipeline(tmp_path, _scope_name()))

    del seeded.repos[REPO].issues[1]
    seeded.repos[REPO].gone[1] = 410
    seeded.repos[REPO].issues[2].update(body="v2", updated_at=at(9))

    class AccessRevokedError(Exception):
        pass

    def check_active():
        if len(seeded.calls) >= stop_after_requests:
            raise AccessRevokedError

    with pytest.raises(Exception) as excinfo:  # dlt wraps it in PipelineStepFailed
        _run_pipeline(tmp_path, seeded, check_active=check_active)
    chain, error = [], excinfo.value
    while error is not None:
        chain.append(error)
        error = error.__cause__ or error.__context__
    assert any(isinstance(error, AccessRevokedError) for error in chain)
    assert _read_documents(_pipeline(tmp_path, _scope_name())) == before

    # The state did not move: the next run does the deletion and the edit.
    documents = _read_documents(_run_pipeline(tmp_path, seeded))
    assert "issue:acme/site#1" not in documents
    assert "v2" in documents["pr:acme/site#2"]


def test_check_active_raising_an_oserror_is_not_retried_or_swallowed(seeded, tmp_path, sleeps):
    # PermissionError is an OSError, like the network errors _get retries and
    # the deletion check keeps; a host's check must still stop the sync.
    _run_pipeline(tmp_path, seeded)
    before = _read_documents(_pipeline(tmp_path, _scope_name()))
    del seeded.repos[REPO].issues[1]
    seeded.repos[REPO].gone[1] = 410

    def check_active():
        if seeded.requested("/issues/1"):  # the deletion check has been asked
            raise PermissionError("access revoked")

    with pytest.raises(Exception) as excinfo:  # dlt wraps it in PipelineStepFailed
        _run_pipeline(tmp_path, seeded, check_active=check_active)
    chain, error = [], excinfo.value
    while error is not None:
        chain.append(error)
        error = error.__cause__ or error.__context__
    assert any(isinstance(error, github.GitHubSyncStoppedError) for error in chain)
    assert any(isinstance(error, PermissionError) for error in chain)
    assert sleeps == []  # not retried
    assert len(seeded.requested("/issues/1")) == 1
    assert _read_documents(_pipeline(tmp_path, _scope_name())) == before

    documents = _read_documents(_run_pipeline(tmp_path, seeded))
    assert "issue:acme/site#1" not in documents  # state did not move


def _scope_name():
    return pipeline_name_for_source(github_source(session=FakeGitHub(), repos=[REPO]), "github_ds")


def test_pipeline_round_trip_edit_rename_and_delete_across_runs(seeded, tmp_path):
    seeded.add_issue(3, updated_at=at(2), body="to be deleted")
    documents = _read_documents(_run_pipeline(tmp_path, seeded))
    assert set(documents) == {
        "repo:acme/site",
        "issue:acme/site#1",
        "pr:acme/site#2",
        "issue:acme/site#3",
        "release:acme/site@501",
    }

    # Between runs: a comment edited on #1, the label on #2 renamed, #3 deleted.
    seeded.repos[REPO].comments[1][0]["body"] = "Reproduced on iOS 17."
    seeded.repos[REPO].issues[1]["updated_at"] = at(9)
    seeded.repos[REPO].issues[2]["labels"] = [{"name": "renamed-label"}]
    del seeded.repos[REPO].issues[3]
    seeded.repos[REPO].gone[3] = 410

    documents = _read_documents(_run_pipeline(tmp_path, seeded))

    assert fetched_issues(seeded) == [1, 2]
    assert set(documents) == {
        "repo:acme/site",
        "issue:acme/site#1",
        "pr:acme/site#2",
        "release:acme/site@501",
    }
    assert "Reproduced on iOS 17." in documents["issue:acme/site#1"]
    assert "Labels: renamed-label" in documents["pr:acme/site#2"]

    # Quiet again. The cursor moved, so the change listing is a new URL once;
    # everything else answers 304.
    _run_pipeline(tmp_path, seeded)
    assert seeded.fresh_calls() == seeded.requested("since=")
    _run_pipeline(tmp_path, seeded)
    assert seeded.fresh_calls() == []


def test_pipeline_issue_that_fails_on_its_own_is_kept_and_retried(seeded, tmp_path):
    _run_pipeline(tmp_path, seeded)
    old_pr = _read_documents(_pipeline(tmp_path, _scope_name()))["pr:acme/site#2"]

    seeded.repos[REPO].issues[1].update(body="v2", updated_at=at(9))
    seeded.repos[REPO].issues[2].update(body="v2", updated_at=at(9))
    for _ in range(github._MAX_RETRIES):  # a persistent 500 on one PR's reviews
        seeded.queue(_error(500), when=lambda url: "/pulls/2/reviews" in url)
    documents = _read_documents(_run_pipeline(tmp_path, seeded))

    assert "v2" in documents["issue:acme/site#1"]  # the run completed
    assert documents["pr:acme/site#2"] == old_pr  # kept as it was, not deleted

    # GitHub recovered. Two things bring #2 back here: its fingerprint stored as
    # "" and, because its updated_at is still inside the change listing's
    # 5-minute overlap, the listing itself. Removing the "" marker therefore
    # leaves this test green; test_two_failures_in_a_row_... checks the marker.
    documents = _read_documents(_run_pipeline(tmp_path, seeded))  # GitHub recovered
    assert fetched_issues(seeded) == [2]
    assert "v2" in documents["pr:acme/site#2"]
    _run_pipeline(tmp_path, seeded)
    assert fetched_issues(seeded) == []  # and settled after that


def _render_failure(status, headers=None):
    return [_error(status, "nope", headers) for _ in range(github._MAX_RETRIES)]


@pytest.mark.parametrize("failure", ["401", "rate limit", "three in a row"])
def test_systemic_render_failure_aborts_with_nothing_committed(seeded, failure):
    for number in (3, 4):
        seeded.add_issue(number, updated_at=at(2))
    state = {}
    run_sync(seeded, state)
    before = copy.deepcopy(state)
    for number in (1, 2, 3, 4):
        seeded.repos[REPO].issues[number]["updated_at"] = at(9)

    def on(number):
        return lambda url: f"/issues/{number}/comments" in url

    if failure == "401":
        seeded.queue(_error(401, "Bad credentials"), when=on(2))
        expected = GitHubAPIError
    elif failure == "rate limit":
        for response in _render_failure(403, {"Retry-After": "1"}):
            seeded.queue(response, when=on(2))
        expected = GitHubAPIError
    else:
        for number in (2, 3, 4):
            for response in _render_failure(500):
                seeded.queue(response, when=on(number))
        expected = github.GitHubUnavailableError

    with pytest.raises(expected):
        run_sync(seeded, state)
    assert state == before


def test_two_failures_in_a_row_are_kept_and_a_success_resets_the_streak(seeded):
    for number in (3, 4, 5):
        seeded.add_issue(number, updated_at=at(2))
    state = {}
    run_sync(seeded, state)
    for number in (1, 2, 3, 4, 5):
        seeded.repos[REPO].issues[number]["updated_at"] = at(9)
    for number in (1, 2, 4, 5):  # 3 succeeds in between
        for response in _render_failure(500):
            seeded.queue(response, when=lambda url, n=number: f"/issues/{n}/comments" in url)

    rows = run_sync(seeded, state)

    assert set(doc_rows(rows, "issue:") | doc_rows(rows, "pr:")) == {"issue:acme/site#3"}
    assert tombstones(rows) == []
    prints = state["repos"][REPO]["fingerprints"]
    assert {n: prints[n] for n in "1245"} == dict.fromkeys("1245", "")


def test_pipeline_abort_mid_render_leaves_destination_and_state_untouched(seeded, tmp_path):
    _run_pipeline(tmp_path, seeded)
    before = _read_documents(_pipeline(tmp_path, _scope_name()))

    seeded.repos[REPO].issues[1].update(body="v2", updated_at=at(9))
    seeded.repos[REPO].issues[2].update(body="v2", updated_at=at(9))
    seeded.queue(_error(401, "Bad credentials"), when=lambda url: "/pulls/2/reviews" in url)
    with pytest.raises(Exception):  # noqa: B017 - dlt wraps the source error in PipelineStepFailed
        _run_pipeline(tmp_path, seeded)

    # Issue 1 was yielded before the 401, but nothing of the run was loaded.
    assert _read_documents(_pipeline(tmp_path, _scope_name())) == before

    documents = _read_documents(_run_pipeline(tmp_path, seeded))
    assert fetched_issues(seeded) == [1, 2]
    assert "v2" in documents["issue:acme/site#1"] and "v2" in documents["pr:acme/site#2"]
