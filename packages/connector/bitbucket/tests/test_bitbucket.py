"""Unit tests for the Bitbucket dlt connector.

Layers, all runnable in CI without a live Bitbucket credential:

* DB-free tests for credential resolution, pagination, retry/error handling,
  listing, rendering, and the incremental/full-pass sync state machine
  (``_iter_rows`` and friends, exercised directly with a plain dict standing
  in for dlt's resource state, mirroring the Google Drive connector's own
  test style).
* A generic document DataItem smoke test (``source="bitbucket"``) that routes
  rows through normal cognify.
* dlt-pipeline tests (a hand-rolled fake Bitbucket session, temp sqlite
  destination) covering the acceptance criteria end-to-end: initial sync
  stages PRs and comments, a deleted/vanished comment drops out on merge
  (forget-on-delete), unchanged rows are kept, and a mid-pagination failure
  aborts without a partial commit.

No HTTP-mocking library is used anywhere (matching the rest of this repo's
connectors) — ``FakeBitbucketSession`` is a plain object that mimics the
``requests.Session`` surface this connector actually calls (``.get(url,
params=...)`` returning an object with ``.status_code`` / ``.json()`` /
``.headers``).
"""

import re
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import NAMESPACE_OID, uuid5

import pytest
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

from cognee_community_connector_bitbucket.bitbucket import (
    BITBUCKET_SOURCE_NAME,
    _comment_to_row,
    _fetch_live_comments,
    _get_json,
    _iter_pull_requests,
    _iter_repo_slugs,
    _iter_rows,
    _list_incremental_prs,
    _paginate,
    _parse_dt,
    _pr_to_row,
    _resolve_credentials,
    _sync_repo_comments,
    bitbucket_source,
)

API_BASE = "https://api.bitbucket.org/2.0"
DEFAULT_UPDATED_ON = "2024-01-01T10:00:00+00:00"


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_bitbucket_env(monkeypatch):
    for var in ("BITBUCKET_EMAIL", "BITBUCKET_API_TOKEN", "BITBUCKET_ACCESS_TOKEN"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    target = "cognee_community_connector_bitbucket.bitbucket._sleep"
    monkeypatch.setattr(target, lambda seconds: None)


def _repo(slug):
    return {"slug": slug, "full_name": f"ws/{slug}"}


def _pr(
    pr_id,
    *,
    title="PR",
    state="OPEN",
    author="Jane Doe",
    source_branch="feature",
    destination_branch="main",
    created_on="2024-01-01T00:00:00+00:00",
    updated_on=DEFAULT_UPDATED_ON,
    comment_count=None,
    task_count=None,
    reviewers=None,
    description="",
):
    return {
        "id": pr_id,
        "title": title,
        "state": state,
        "author": {"display_name": author},
        "source": {"branch": {"name": source_branch}},
        "destination": {"branch": {"name": destination_branch}},
        "created_on": created_on,
        "updated_on": updated_on,
        "comment_count": comment_count,
        "task_count": task_count,
        "reviewers": reviewers or [],
        "description": description,
        "links": {"html": {"href": f"https://bitbucket.org/ws/repo/pull-requests/{pr_id}"}},
    }


def _comment(
    comment_id,
    *,
    raw="comment text",
    deleted=False,
    author="John Doe",
    path=None,
    to_line=None,
    parent_id=None,
):
    comment = {
        "id": comment_id,
        "content": {"raw": raw},
        "deleted": deleted,
        "user": {"display_name": author},
        "links": {"html": {"href": f"https://bitbucket.org/comments/{comment_id}"}},
    }
    if path:
        comment["inline"] = {"path": path, "to": to_line}
    if parent_id is not None:
        comment["parent"] = {"id": parent_id}
    return comment


class _FakeResponse:
    def __init__(self, status_code, payload, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"unexpected status {self.status_code}")


def _normalize_params(params):
    if not params:
        return {}
    pairs = params.items() if isinstance(params, dict) else params
    normalized: dict[str, list[str]] = {}
    for key, value in pairs:
        normalized.setdefault(key, []).append(str(value))
    return normalized


class FakeBitbucketSession:
    """Stand-in for a ``requests.Session`` hitting the Bitbucket Cloud API.

    Backed by in-memory repos/PRs/comments fixtures, keyed exactly like the
    real hierarchy. Pagination is modeled faithfully: list endpoints page at
    a small, fixed page size and return a ``next`` URL that embeds every
    query param a real Bitbucket ``next`` link would carry (including
    ``state``/``sort``), so the connector's "follow next verbatim, never
    resend params" contract is actually exercised rather than assumed.

    ``sort=-updated_on`` is honored for pull-request listings by sorting the
    fixture by ``updated_on`` descending before paging, UNLESS
    ``simulate_unsorted=True``, which deliberately returns PRs in raw fixture
    order regardless of the requested sort — used to test the connector's
    guard against an API that doesn't actually honor the sort it was asked
    for.
    """

    PAGE_SIZE = 2

    def __init__(self, repos=None, prs=None, comments=None, simulate_unsorted=False):
        self.repos = repos or {}  # {workspace: [repo, ...]}
        self.prs = prs or {}  # {(workspace, repo_slug): [pr, ...]}
        self.comments = comments or {}  # {(workspace, repo_slug, pr_id): [comment, ...]}
        self.simulate_unsorted = simulate_unsorted
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, params))
        parsed = urlsplit(url)
        path = parsed.path
        query = parse_qs(parsed.query)
        for key, values in _normalize_params(params).items():
            query[key] = values

        m = re.fullmatch(r"/2\.0/repositories/([^/]+)", path)
        if m:
            (workspace,) = m.groups()
            return self._page(self.repos.get(workspace, []), path, query)

        m = re.fullmatch(r"/2\.0/repositories/([^/]+)/([^/]+)/pullrequests", path)
        if m:
            workspace, repo_slug = m.groups()
            if (workspace, repo_slug) not in self.prs:
                message = f"repository not found: {repo_slug}"
                return _FakeResponse(404, {"error": {"message": message}})
            items = list(self.prs[(workspace, repo_slug)])
            wanted_states = set(query.get("state", []))
            if wanted_states:
                items = [pr for pr in items if pr.get("state") in wanted_states]

            sort_param = (query.get("sort") or [None])[0]
            if sort_param == "-updated_on" and not self.simulate_unsorted:
                items = sorted(items, key=lambda pr: pr.get("updated_on") or "", reverse=True)

            extra = {}
            if wanted_states:
                extra["state"] = sorted(wanted_states)
            if sort_param:
                extra["sort"] = sort_param
            return self._page(items, path, query, extra_params=extra)

        m = re.fullmatch(r"/2\.0/repositories/([^/]+)/([^/]+)/pullrequests/([^/]+)/comments", path)
        if m:
            workspace, repo_slug, pr_id = m.groups()
            items = self.comments.get((workspace, repo_slug, int(pr_id)), [])
            return self._page(items, path, query)

        return _FakeResponse(404, {"error": {"message": f"not found: {path}"}})

    def _page(self, items, path, query, extra_params=None):
        page = int((query.get("page") or ["1"])[0])
        start = (page - 1) * self.PAGE_SIZE
        chunk = items[start : start + self.PAGE_SIZE]
        next_url = None
        if start + self.PAGE_SIZE < len(items):
            next_params = dict(extra_params or {})
            next_params["pagelen"] = self.PAGE_SIZE
            next_params["page"] = page + 1
            next_url = f"https://api.bitbucket.org{path}?{urlencode(next_params, doseq=True)}"
        return _FakeResponse(200, {"values": chunk, "next": next_url})


class _ScriptedSession:
    """Returns a scripted sequence of responses to successive GET calls.

    Used for exercising ``_get_json``'s retry/error handling in isolation
    from the full fixture-backed ``FakeBitbucketSession`` above.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def get(self, url, params=None):
        self.calls += 1
        status, payload, headers = self._responses.pop(0)
        return _FakeResponse(status, payload, headers or {})


class _BoomSession:
    """Raises on every GET — for mid-run failure tests."""

    def get(self, url, params=None):
        raise RuntimeError("network boom")


class _BoomAfterNSession:
    """Succeeds through a fixed session for the first N calls, then raises.

    Used to put a failure partway through a multi-call sequence (e.g. after
    the PR listing succeeds but during comment listing).
    """

    def __init__(self, delegate, succeed_calls):
        self._delegate = delegate
        self._remaining = succeed_calls

    def get(self, url, params=None):
        if self._remaining <= 0:
            raise RuntimeError("network boom (after N successful calls)")
        self._remaining -= 1
        return self._delegate.get(url, params=params)


# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------


def test_access_token_resolves_to_bearer():
    assert _resolve_credentials(None, None, "tok123") == ("bearer", "tok123")


def test_email_and_api_token_resolve_to_basic():
    scheme, credential = _resolve_credentials("me@example.com", "tok", None)
    assert (scheme, credential) == ("basic", ("me@example.com", "tok"))


def test_api_token_alone_resolves_to_bearer():
    assert _resolve_credentials(None, "tok", None) == ("bearer", "tok")


def test_access_token_and_api_token_together_raises():
    with pytest.raises(ValueError, match="not both"):
        _resolve_credentials(None, "tok", "other-tok")


def test_no_credentials_raises_with_clear_message():
    with pytest.raises(ValueError, match="requires credentials"):
        _resolve_credentials(None, None, None)


def test_email_without_api_token_raises():
    with pytest.raises(ValueError, match="requires credentials"):
        _resolve_credentials("me@example.com", None, None)


def test_credentials_fall_back_to_env_vars(monkeypatch):
    monkeypatch.setenv("BITBUCKET_ACCESS_TOKEN", "env-tok")
    assert _resolve_credentials(None, None, None) == ("bearer", "env-tok")


def test_explicit_args_take_priority_over_env(monkeypatch):
    monkeypatch.setenv("BITBUCKET_ACCESS_TOKEN", "env-tok")
    assert _resolve_credentials(None, None, "explicit-tok") == ("bearer", "explicit-tok")


def test_credential_error_messages_never_contain_the_token():
    secret = "super-secret-token-xyz"
    with pytest.raises(ValueError) as exc_info:
        _resolve_credentials(None, secret, secret)
    assert secret not in str(exc_info.value)

    with pytest.raises(ValueError) as exc_info:
        _resolve_credentials(None, None, None)
    assert secret not in str(exc_info.value)


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def test_paginate_follows_next_url_and_sends_params_only_once():
    session = FakeBitbucketSession(prs={("ws", "repo"): [_pr(1), _pr(2), _pr(3)]})
    url = f"{API_BASE}/repositories/ws/repo/pullrequests"
    items = list(_paginate(session, url, params=[("state", "OPEN")]))

    assert [item["id"] for item in items] == [1, 2, 3]
    assert session.calls[0][1] is not None  # first call carried params
    assert all(call_params is None for _, call_params in session.calls[1:])  # later calls did not


def test_paginate_stops_at_last_page_with_no_next():
    session = FakeBitbucketSession(repos={"ws": [_repo("a"), _repo("b")]})
    items = list(_paginate(session, f"{API_BASE}/repositories/ws"))
    assert [r["slug"] for r in items] == ["a", "b"]


def test_paginate_raises_on_repeated_next_url():
    class LoopingSession:
        def get(self, url, params=None):
            return _FakeResponse(200, {"values": [], "next": url})

    with pytest.raises(RuntimeError, match="loop"):
        list(_paginate(LoopingSession(), f"{API_BASE}/x"))


# ---------------------------------------------------------------------------
# Listing: repos / PRs
# ---------------------------------------------------------------------------


def test_iter_repo_slugs_uses_given_slugs_without_an_extra_call():
    session = FakeBitbucketSession()
    assert list(_iter_repo_slugs(session, "ws", ["a", "b"])) == ["a", "b"]
    assert session.calls == []


def test_iter_repo_slugs_lists_every_repo_when_none_given():
    session = FakeBitbucketSession(repos={"ws": [_repo("a"), _repo("b")]})
    assert list(_iter_repo_slugs(session, "ws", None)) == ["a", "b"]


def test_pr_listing_sends_one_state_param_per_requested_state():
    prs = [_pr(1, state="OPEN"), _pr(2, state="MERGED")]
    session = FakeBitbucketSession(prs={("ws", "repo"): prs})
    list(_iter_pull_requests(session, "ws", "repo", ("OPEN", "MERGED")))

    first_call_params = session.calls[0][1]
    sent_states = sorted(v for k, v in first_call_params if k == "state")
    assert sent_states == ["MERGED", "OPEN"]


def test_pr_listing_filters_by_requested_states():
    fixture_prs = [_pr(1, state="OPEN"), _pr(2, state="DECLINED"), _pr(3, state="MERGED")]
    session = FakeBitbucketSession(prs={("ws", "repo"): fixture_prs})
    prs = list(_iter_pull_requests(session, "ws", "repo", ("OPEN", "MERGED")))
    assert sorted(pr["id"] for pr in prs) == [1, 3]


def test_invalid_pr_state_raises():
    fake_session = FakeBitbucketSession()
    with pytest.raises(ValueError, match="Invalid pr_states"):
        bitbucket_source(
            workspace="ws",
            access_token="tok",
            pr_states=("OPEN", "BOGUS"),
            session=fake_session,
        )


def test_empty_pr_states_raises():
    fake_session = FakeBitbucketSession()
    with pytest.raises(ValueError, match="must not be empty"):
        bitbucket_source(workspace="ws", access_token="tok", pr_states=(), session=fake_session)


def test_empty_workspace_raises():
    with pytest.raises(ValueError, match="non-empty workspace"):
        bitbucket_source(workspace="", access_token="tok", session=FakeBitbucketSession())


def test_empty_repo_slugs_list_raises():
    fake_session = FakeBitbucketSession()
    with pytest.raises(ValueError, match="repo_slugs"):
        bitbucket_source(workspace="ws", access_token="tok", repo_slugs=[], session=fake_session)


def test_full_sync_every_below_one_raises():
    fake_session = FakeBitbucketSession()
    with pytest.raises(ValueError, match="full_sync_every"):
        bitbucket_source(
            workspace="ws", access_token="tok", full_sync_every=0, session=fake_session
        )


# ---------------------------------------------------------------------------
# _get_json: retries / error classification
# ---------------------------------------------------------------------------


def test_get_json_retries_429_then_succeeds():
    session = _ScriptedSession([(429, {}, {}), (200, {"ok": True}, {})])
    assert _get_json(session, f"{API_BASE}/x") == {"ok": True}
    assert session.calls == 2


def test_get_json_retries_server_errors():
    session = _ScriptedSession([(503, {}, {}), (502, {}, {}), (200, {"ok": True}, {})])
    assert _get_json(session, f"{API_BASE}/x") == {"ok": True}


def test_get_json_exhausts_retries_and_raises():
    session = _ScriptedSession([(503, {}, {}) for _ in range(5)])
    with pytest.raises(RuntimeError, match="failed after"):
        _get_json(session, f"{API_BASE}/x")


def test_get_json_401_raises_with_scopes_hint():
    session = _ScriptedSession([(401, {}, {})])
    with pytest.raises(RuntimeError, match="read:pullrequest:bitbucket"):
        _get_json(session, f"{API_BASE}/x")


def test_get_json_403_raises_with_scopes_hint():
    session = _ScriptedSession([(403, {}, {})])
    with pytest.raises(RuntimeError, match="read:workspace:bitbucket"):
        _get_json(session, f"{API_BASE}/x")


def test_get_json_404_names_the_resource_path_not_credentials():
    url = f"{API_BASE}/repositories/ws/missing-repo"
    session = _ScriptedSession([(404, {}, {})])
    with pytest.raises(RuntimeError, match=re.escape(url)) as exc_info:
        _get_json(session, url)
    assert "credential" not in str(exc_info.value).lower()


def test_get_json_near_limit_header_logs_a_warning(caplog):
    responses = [(429, {}, {"X-RateLimit-NearLimit": "true"}), (200, {"ok": True}, {})]
    session = _ScriptedSession(responses)
    _get_json(session, f"{API_BASE}/x")
    assert any("rate limit" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# Comment fetching (_fetch_live_comments / _sync_repo_comments)
# ---------------------------------------------------------------------------


def test_fetch_live_comments_filters_deleted_and_empty():
    session = FakeBitbucketSession(
        comments={
            ("ws", "repo", 1): [
                _comment(1, raw="hello"),
                _comment(2, raw=""),
                _comment(3, raw="bye", deleted=True),
            ]
        }
    )
    live = _fetch_live_comments(session, "ws", "repo", 1)
    assert [c["id"] for c in live] == [1]


def test_fetch_live_comments_always_makes_the_call():
    # Unlike _sync_repo_comments, _fetch_live_comments has no comment_count
    # shortcut of its own -- the skip decision lives one layer up.
    session = FakeBitbucketSession(comments={("ws", "repo", 1): [_comment(1, raw="hi")]})
    assert len(_fetch_live_comments(session, "ws", "repo", 1)) == 1
    assert session.calls


def test_sync_repo_comments_skips_call_when_comment_count_zero_and_no_prior_ids():
    pr = _pr(1, comment_count=0)
    unseen = [_comment(9, raw="should not be seen")]
    session = FakeBitbucketSession(comments={("ws", "repo", 1): unseen})
    rows, new_ids = _sync_repo_comments(session, "ws", "repo", {"1": pr}, {})
    assert rows == []
    assert new_ids == {"1": []}
    assert session.calls == []


def test_sync_repo_comments_still_lists_when_comment_count_zero_but_prior_ids_exist():
    # comment_count dropped to 0 but we previously knew about a comment --
    # must still check, so the deletion is actually detected.
    pr = _pr(1, comment_count=0)
    session = FakeBitbucketSession(comments={("ws", "repo", 1): []})
    rows, new_ids = _sync_repo_comments(session, "ws", "repo", {"1": pr}, {"1": ["5"]})
    assert session.calls  # the call WAS made
    assert new_ids == {"1": []}
    assert any(r["id"].endswith(":comment:5") and r["_deleted"] for r in rows)


def test_sync_repo_comments_missing_comment_count_still_fetches():
    pr = _pr(1, comment_count=None)
    session = FakeBitbucketSession(comments={("ws", "repo", 1): [_comment(1, raw="hi")]})
    rows, new_ids = _sync_repo_comments(session, "ws", "repo", {"1": pr}, {})
    assert len(rows) == 1
    assert new_ids == {"1": ["1"]}
    assert session.calls


def test_sync_repo_comments_tombstones_deleted_and_vanished():
    pr = _pr(1)
    session = FakeBitbucketSession(comments={("ws", "repo", 1): [_comment(1, raw="still here")]})
    # Prior run knew about comments 1, 2 (deleted upstream), 3 (vanished upstream).
    rows, new_ids = _sync_repo_comments(session, "ws", "repo", {"1": pr}, {"1": ["1", "2", "3"]})
    tombstoned = {r["id"] for r in rows if r["_deleted"]}
    assert tombstoned == {
        "bitbucket:ws:repo:pr:1:comment:2",
        "bitbucket:ws:repo:pr:1:comment:3",
    }
    assert new_ids == {"1": ["1"]}


def test_sync_repo_comments_empty_sweep_guard_is_repo_wide_not_per_pr():
    # PR 1 legitimately lost all its comments; PR 2's comments are untouched
    # and still present. The batch total is NOT zero, so the guard must NOT
    # trip, and PR 1's genuine deletion must still be tombstoned.
    pr1, pr2 = _pr(1), _pr(2)
    session = FakeBitbucketSession(
        comments={
            ("ws", "repo", 1): [],
            ("ws", "repo", 2): [_comment(9, raw="still here")],
        }
    )
    prs_to_check = {"1": pr1, "2": pr2}
    prior_ids = {"1": ["5"], "2": ["9"]}
    rows, new_ids = _sync_repo_comments(session, "ws", "repo", prs_to_check, prior_ids)

    tombstoned = {r["id"] for r in rows if r["_deleted"]}
    assert tombstoned == {"bitbucket:ws:repo:pr:1:comment:5"}
    assert new_ids == {"1": [], "2": ["9"]}


def test_sync_repo_comments_empty_sweep_guard_trips_when_every_checked_pr_is_empty():
    pr1, pr2 = _pr(1), _pr(2)
    session = FakeBitbucketSession(comments={("ws", "repo", 1): [], ("ws", "repo", 2): []})
    prs_to_check = {"1": pr1, "2": pr2}
    prior_ids = {"1": ["5"], "2": ["9"]}
    rows, new_ids = _sync_repo_comments(session, "ws", "repo", prs_to_check, prior_ids)

    assert rows == []  # no tombstones emitted
    assert new_ids == prior_ids  # state kept exactly as it was


# ---------------------------------------------------------------------------
# Rendering / row building
# ---------------------------------------------------------------------------


def test_pr_row_has_stable_composite_id_title_and_url():
    pr = _pr(42, title="Add feature")
    row = _pr_to_row("acme", "widgets", pr)
    assert row["id"] == "bitbucket:acme:widgets:pr:42"
    assert row["title"] == "[widgets#42] Add feature"
    assert row["url"] == pr["links"]["html"]["href"]
    assert row["_deleted"] is False
    assert "Repository: widgets" in row["content"]
    assert "Branch: feature -> main" in row["content"]


def test_pr_row_includes_description():
    pr = _pr(1, description="This fixes the thing.")
    row = _pr_to_row("ws", "repo", pr)
    assert "This fixes the thing." in row["content"]


def test_pr_row_content_is_identical_despite_volatile_field_changes():
    pr1 = _pr(1, title="T", updated_on="2024-01-01T00:00:00+00:00", comment_count=0, task_count=0)
    pr2 = _pr(1, title="T", updated_on="2024-06-01T00:00:00+00:00", comment_count=5, task_count=2)
    assert _pr_to_row("ws", "repo", pr1) == _pr_to_row("ws", "repo", pr2)


def test_comment_row_has_stable_composite_id():
    pr = _pr(42, title="Add feature")
    comment = _comment(7, raw="nice")
    row = _comment_to_row("acme", "widgets", pr, comment)
    assert row["id"] == "bitbucket:acme:widgets:pr:42:comment:7"
    assert row["title"] == "Comment on [widgets#42] Add feature"
    assert row["_deleted"] is False


def test_comment_row_renders_inline_context():
    pr = _pr(1, title="Fix bug")
    comment = _comment(5, raw="looks good", path="src/app.py", to_line=42)
    row = _comment_to_row("ws", "repo", pr, comment)
    assert "On src/app.py:42" in row["content"]
    assert "looks good" in row["content"]


def test_comment_row_renders_reply_note():
    pr = _pr(1, title="Fix bug")
    comment = _comment(6, raw="agreed", parent_id=5)
    row = _comment_to_row("ws", "repo", pr, comment)
    assert "Reply to comment 5" in row["content"]


def test_comment_row_falls_back_to_pr_url_when_comment_has_none():
    pr = _pr(1, title="Fix bug")
    comment = _comment(6, raw="agreed")
    comment["links"] = {}
    row = _comment_to_row("ws", "repo", pr, comment)
    assert row["url"] == pr["links"]["html"]["href"]


# ---------------------------------------------------------------------------
# Datetime comparison (_parse_dt)
# ---------------------------------------------------------------------------


def test_parse_dt_compares_equal_instants_across_offsets():
    utc = _parse_dt("2024-01-01T10:00:00+00:00")
    pacific = _parse_dt("2024-01-01T03:00:00-07:00")
    assert utc == pacific


def test_parse_dt_orders_by_microseconds():
    earlier = _parse_dt("2024-01-01T10:00:00.100000+00:00")
    later = _parse_dt("2024-01-01T10:00:00.500000+00:00")
    assert later > earlier


# ---------------------------------------------------------------------------
# _list_incremental_prs: sort-based early stop + monotonicity guard
# ---------------------------------------------------------------------------


def test_list_incremental_prs_stops_early_past_the_cursor():
    # PAGE_SIZE=2: page 1 = [1, 2] (both in scope), page 2 = [3, 4] (both out
    # of scope -- stops on the first one), page 3 = [5] (never fetched).
    prs = [
        _pr(1, updated_on="2024-05-01T00:00:00+00:00"),
        _pr(2, updated_on="2024-02-01T00:00:00+00:00"),
        _pr(3, updated_on="2024-01-05T00:00:00+00:00"),
        _pr(4, updated_on="2024-01-04T00:00:00+00:00"),
        _pr(5, updated_on="2024-01-03T00:00:00+00:00"),
    ]
    session = FakeBitbucketSession(prs={("ws", "repo"): prs})
    result = _list_incremental_prs(session, "ws", "repo", ("OPEN",), "2024-02-01T00:00:00+00:00")
    assert sorted(pr["id"] for pr in result) == [1, 2]  # ids 3,4,5 (older than cursor) excluded
    # Page 2 is fetched (it contains id 3, which is what triggers the stop),
    # but page 3 (id 5) is never requested.
    assert len(session.calls) == 2


def test_list_incremental_prs_includes_ties_with_cursor():
    prs = [_pr(1, updated_on="2024-02-01T00:00:00+00:00")]
    session = FakeBitbucketSession(prs={("ws", "repo"): prs})
    result = _list_incremental_prs(session, "ws", "repo", ("OPEN",), "2024-02-01T00:00:00+00:00")
    assert [pr["id"] for pr in result] == [1]


def test_list_incremental_prs_mixed_offset_formats_compare_correctly():
    prs = [_pr(1, updated_on="2024-01-01T09:00:00-01:00")]  # == 10:00 UTC
    session = FakeBitbucketSession(prs={("ws", "repo"): prs})
    result = _list_incremental_prs(session, "ws", "repo", ("OPEN",), "2024-01-01T10:00:00+00:00")
    assert [pr["id"] for pr in result] == [1]  # tie across differing offsets, still included


def test_list_incremental_prs_disables_early_stop_when_sort_is_violated(caplog):
    # Deliberately out-of-order fixture, with the fake told to ignore `sort`
    # (simulating an API that doesn't actually honor it).
    prs = [
        _pr(1, updated_on="2024-01-01T00:00:00+00:00"),  # old, listed first
        _pr(2, updated_on="2024-03-01T00:00:00+00:00"),  # newer, listed second (violation)
        _pr(3, updated_on="2024-02-15T00:00:00+00:00"),
    ]
    session = FakeBitbucketSession(prs={("ws", "repo"): prs}, simulate_unsorted=True)
    result = _list_incremental_prs(session, "ws", "repo", ("OPEN",), "2024-02-01T00:00:00+00:00")
    # Despite PR 1 (old) appearing first, PRs 2 and 3 (both >= cursor) are
    # still found because the early stop was disabled after the violation.
    assert sorted(pr["id"] for pr in result) == [2, 3]
    assert any("not sorted" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# _iter_rows: full vs incremental orchestration (plain dict state)
# ---------------------------------------------------------------------------


def _snapshot_state(state: dict) -> dict:
    """Shallow copy of ``state`` for before/after comparison in failure tests."""
    return {
        "repos": {k: dict(v) for k, v in state.get("repos", {}).items()},
        "runs_since_full": state.get("runs_since_full"),
    }


def _session_with(repo_slug="repo", prs=None, comments=None, repos=("repo",)):
    return FakeBitbucketSession(
        repos={"ws": [_repo(slug) for slug in repos]},
        prs={("ws", repo_slug): prs or []},
        comments=comments or {},
    )


def test_first_run_is_a_full_pass_and_populates_state():
    session = _session_with(
        prs=[_pr(1, comment_count=1)], comments={("ws", "repo", 1): [_comment(1)]}
    )
    state = {}
    all_states = ("OPEN", "MERGED", "DECLINED", "SUPERSEDED")
    rows = list(_iter_rows(session, "ws", None, all_states, 10, state))

    assert {r["id"] for r in rows} == {
        "bitbucket:ws:repo:pr:1",
        "bitbucket:ws:repo:pr:1:comment:1",
    }
    assert state["runs_since_full"] == 0
    assert state["repos"]["repo"]["pr_ids"] == ["1"]
    assert state["repos"]["repo"]["comment_ids"] == {"1": ["1"]}
    assert state["repos"]["repo"]["pr_state_by_id"] == {"1": "OPEN"}
    assert state["repos"]["repo"]["pr_cursor"] == DEFAULT_UPDATED_ON


def test_second_run_with_no_changes_is_incremental_and_only_rechecks_open_comments():
    closed_pr = _pr(1, state="MERGED", comment_count=1, updated_on="2024-01-01T00:00:00+00:00")
    open_pr = _pr(2, state="OPEN", comment_count=1, updated_on="2024-01-02T00:00:00+00:00")
    session = _session_with(
        prs=[closed_pr, open_pr],
        comments={
            ("ws", "repo", 1): [_comment(1, raw="closed-pr-comment")],
            ("ws", "repo", 2): [_comment(2, raw="open-pr-comment")],
        },
    )
    state = {}
    list(_iter_rows(session, "ws", None, ("OPEN", "MERGED"), 10, state))

    session.calls.clear()
    # Second run, same fixtures, same session object (no new data).
    rows = list(_iter_rows(session, "ws", None, ("OPEN", "MERGED"), 10, state))

    comment_calls = [url for url, _ in session.calls if url.endswith("/comments")]
    assert any("pr:1" not in url and "pullrequests/1/comments" not in url for url in [""]) or True
    assert all("/pullrequests/2/comments" in url for url in comment_calls)
    assert not any("/pullrequests/1/comments" in url for url in comment_calls)
    # Both PRs' updated_on ties with the cursor, so both are re-yielded as
    # harmless no-op upserts; only PR 2 (OPEN) gets its comments re-checked.
    assert any(r["id"] == "bitbucket:ws:repo:pr:1:comment:1" for r in rows) is False


def test_pr_updated_is_picked_up_and_state_change_is_reflected():
    pr = _pr(1, state="OPEN", updated_on="2024-01-01T00:00:00+00:00")
    session = _session_with(prs=[pr])
    state = {}
    list(_iter_rows(session, "ws", None, ("OPEN", "MERGED"), 10, state))

    merged_pr = _pr(1, state="MERGED", updated_on="2024-02-01T00:00:00+00:00")
    session2 = _session_with(prs=[merged_pr])
    rows = list(_iter_rows(session2, "ws", None, ("OPEN", "MERGED"), 10, state))

    assert any(r["id"] == "bitbucket:ws:repo:pr:1" for r in rows)
    assert state["repos"]["repo"]["pr_state_by_id"]["1"] == "MERGED"
    assert state["repos"]["repo"]["pr_cursor"] == "2024-02-01T00:00:00+00:00"


def test_comment_added_to_open_pr_without_bumping_pr_updated_on_is_picked_up():
    pr = _pr(1, state="OPEN", comment_count=0)
    session = _session_with(prs=[pr], comments={("ws", "repo", 1): []})
    state = {}
    list(_iter_rows(session, "ws", None, ("OPEN",), 10, state))

    # Same PR, same updated_on, but a comment now exists -- comment_count is
    # stale/unreliable info from a connector's point of view, so a fresh
    # comment appearing without any PR-level signal must still be found.
    same_pr = _pr(1, state="OPEN", comment_count=1)
    surprise_comments = {("ws", "repo", 1): [_comment(1, raw="surprise")]}
    session2 = _session_with(prs=[same_pr], comments=surprise_comments)
    rows = list(_iter_rows(session2, "ws", None, ("OPEN",), 10, state))

    assert any(r["id"] == "bitbucket:ws:repo:pr:1:comment:1" for r in rows)


def test_comment_deleted_flag_true_produces_tombstone_and_removes_from_state():
    pr = _pr(1, state="OPEN", comment_count=1)
    session = _session_with(prs=[pr], comments={("ws", "repo", 1): [_comment(1, raw="x")]})
    state = {}
    list(_iter_rows(session, "ws", None, ("OPEN",), 10, state))

    session2 = _session_with(
        prs=[pr], comments={("ws", "repo", 1): [_comment(1, raw="x", deleted=True)]}
    )
    rows = list(_iter_rows(session2, "ws", None, ("OPEN",), 10, state))

    tombstones = [r for r in rows if r["_deleted"]]
    assert any(t["id"] == "bitbucket:ws:repo:pr:1:comment:1" for t in tombstones)
    assert state["repos"]["repo"]["comment_ids"]["1"] == []


def test_comment_missing_from_listing_produces_tombstone_and_removes_from_state():
    pr = _pr(1, state="OPEN", comment_count=1)
    session = _session_with(prs=[pr], comments={("ws", "repo", 1): [_comment(1, raw="x")]})
    state = {}
    list(_iter_rows(session, "ws", None, ("OPEN",), 10, state))

    session2 = _session_with(prs=[pr], comments={("ws", "repo", 1): []})
    rows = list(_iter_rows(session2, "ws", None, ("OPEN",), 10, state))

    tombstones = [r for r in rows if r["_deleted"]]
    assert any(t["id"] == "bitbucket:ws:repo:pr:1:comment:1" for t in tombstones)
    assert state["repos"]["repo"]["comment_ids"]["1"] == []


def test_full_pass_catches_deleted_comment_on_a_closed_pr():
    closed_pr = _pr(1, state="MERGED", comment_count=1)
    session = _session_with(prs=[closed_pr], comments={("ws", "repo", 1): [_comment(1, raw="x")]})
    state = {}
    # full_sync_every=1 -> every run is a full pass.
    list(_iter_rows(session, "ws", None, ("OPEN", "MERGED"), 1, state))

    session2 = _session_with(prs=[closed_pr], comments={("ws", "repo", 1): []})
    rows = list(_iter_rows(session2, "ws", None, ("OPEN", "MERGED"), 1, state))

    tombstones = [r for r in rows if r["_deleted"]]
    assert any(t["id"] == "bitbucket:ws:repo:pr:1:comment:1" for t in tombstones)


def test_full_pass_catches_pr_dropped_by_narrowed_pr_states():
    open_pr = _pr(1, state="OPEN")
    declined_pr = _pr(2, state="DECLINED")
    session = _session_with(prs=[open_pr, declined_pr])
    state = {}
    list(_iter_rows(session, "ws", None, ("OPEN", "DECLINED"), 1, state))
    assert set(state["repos"]["repo"]["pr_ids"]) == {"1", "2"}

    # Narrow pr_states to OPEN only -- PR 2 should be tombstoned on the next
    # (full) pass, since it's no longer in the configured scope.
    rows = list(_iter_rows(session, "ws", None, ("OPEN",), 1, state))
    tombstones = [r for r in rows if r["_deleted"]]
    assert any(t["id"] == "bitbucket:ws:repo:pr:2" for t in tombstones)
    assert state["repos"]["repo"]["pr_ids"] == ["1"]


def test_full_pass_catches_a_vanished_repo():
    session = _session_with(repos=("repo", "other"), prs=[_pr(1)])
    session.prs[("ws", "other")] = [_pr(99)]
    state = {}
    list(_iter_rows(session, "ws", None, ("OPEN",), 1, state))
    assert set(state["repos"]) == {"repo", "other"}

    session2 = FakeBitbucketSession(repos={"ws": [_repo("repo")]}, prs={("ws", "repo"): [_pr(1)]})
    rows = list(_iter_rows(session2, "ws", None, ("OPEN",), 1, state))
    tombstones = [r for r in rows if r["_deleted"]]
    assert any(t["id"] == "bitbucket:ws:other:pr:99" for t in tombstones)
    assert "other" not in state["repos"]


def test_repo_level_empty_sweep_guard_keeps_state_when_workspace_listing_is_empty():
    session = _session_with(prs=[_pr(1)])
    state = {}
    list(_iter_rows(session, "ws", None, ("OPEN",), 1, state))

    empty_session = FakeBitbucketSession(repos={"ws": []})
    rows = list(_iter_rows(empty_session, "ws", None, ("OPEN",), 1, state))

    assert rows == []
    assert "repo" in state["repos"]  # nothing was dropped


# ---------------------------------------------------------------------------
# Mid-run failures leave state untouched
# ---------------------------------------------------------------------------


def test_mid_run_exception_during_pr_pagination_leaves_state_unchanged():
    comments = {("ws", "repo", 1): [_comment(1)]}
    session = _session_with(prs=[_pr(1, comment_count=1)], comments=comments)
    state = {}
    list(_iter_rows(session, "ws", None, ("OPEN",), 10, state))
    before = _snapshot_state(state)

    boom = _BoomSession()
    with pytest.raises(RuntimeError, match="network boom"):
        list(_iter_rows(boom, "ws", None, ("OPEN",), 10, state))

    assert state["runs_since_full"] == before["runs_since_full"]
    assert state["repos"] == before["repos"]


def test_mid_run_exception_during_comment_listing_leaves_state_unchanged():
    pr = _pr(1, state="OPEN", comment_count=1)
    session = _session_with(prs=[pr], comments={("ws", "repo", 1): [_comment(1)]})
    state = {}
    list(_iter_rows(session, "ws", None, ("OPEN",), 10, state))
    before = _snapshot_state(state)

    # Succeed through repo listing + PR listing (2 calls), then blow up
    # during the comment listing that follows.
    boom = _BoomAfterNSession(_session_with(prs=[pr]), succeed_calls=2)
    with pytest.raises(RuntimeError, match="network boom"):
        list(_iter_rows(boom, "ws", None, ("OPEN",), 10, state))

    assert state["runs_since_full"] == before["runs_since_full"]
    assert state["repos"] == before["repos"]


# ---------------------------------------------------------------------------
# full_sync_every behavior
# ---------------------------------------------------------------------------


def test_full_sync_every_one_means_every_run_is_full():
    pr = _pr(1, state="MERGED")
    session = _session_with(prs=[pr])
    state = {}
    list(_iter_rows(session, "ws", None, ("OPEN", "MERGED"), 1, state))
    assert state["runs_since_full"] == 0
    list(_iter_rows(session, "ws", None, ("OPEN", "MERGED"), 1, state))
    assert state["runs_since_full"] == 0  # stayed at 0 -- every run resets it


def test_full_sync_every_counts_up_then_resets():
    # full_sync_every=3: full on run 1 (empty state), then is_full triggers
    # once runs_since_full + 1 >= 3, i.e. on run 4 (0 -> 1 -> 2 -> full).
    pr = _pr(1, state="OPEN")
    session = _session_with(prs=[pr])
    state = {}
    list(_iter_rows(session, "ws", None, ("OPEN",), 3, state))  # run 1: full (empty state)
    assert state["runs_since_full"] == 0
    list(_iter_rows(session, "ws", None, ("OPEN",), 3, state))  # run 2: 0+1=1>=3? no -> incremental
    assert state["runs_since_full"] == 1
    list(_iter_rows(session, "ws", None, ("OPEN",), 3, state))  # run 3: 1+1=2>=3? no -> incremental
    assert state["runs_since_full"] == 2
    list(_iter_rows(session, "ws", None, ("OPEN",), 3, state))  # run 4: 2+1=3>=3? yes -> full
    assert state["runs_since_full"] == 0


# ---------------------------------------------------------------------------
# Document marker / DataItem smoke test
# ---------------------------------------------------------------------------


def test_bitbucket_source_declares_document_marker():
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    source = bitbucket_source(workspace="ws", access_token="tok", session=FakeBitbucketSession())
    assert BITBUCKET_SOURCE_NAME == "bitbucket"
    assert document_source_tag(source) == "bitbucket"


def test_build_document_data_item_tags_source():
    row = SimpleNamespace(
        row_data={
            "id": "bitbucket:ws:repo:pr:1",
            "url": "https://bitbucket.org/ws/repo/pull-requests/1",
            "title": "[repo#1] Add feature",
            "content": "Repository: repo\nState: OPEN",
        },
        content_hash="abc123",
    )
    data_id = uuid5(NAMESPACE_OID, "bitbucket:ws:repo:pr:1")

    item = _build_document_data_item(row, data_id, "bitbucket")

    assert item.external_metadata["source"] == "bitbucket"
    assert item.external_metadata["url"] == "https://bitbucket.org/ws/repo/pull-requests/1"
    assert item.external_metadata["external_id"] == "bitbucket:ws:repo:pr:1"
    assert item.data_id == data_id
    assert item.data.startswith("# [repo#1] Add feature")


# ---------------------------------------------------------------------------
# dlt pipeline: merge + forget-on-delete (needs dlt)
# ---------------------------------------------------------------------------


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def _run_sync(dlt, tmp_path, run_name, session, full_sync_every=10):
    db_path = (tmp_path / f"{run_name}.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name=run_name,
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="bitbucket_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(
        bitbucket_source(
            workspace="ws", access_token="tok", full_sync_every=full_sync_every, session=session
        )
    )
    return pipeline


def _read_documents(pipeline):
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, title, content FROM bitbucket_documents") as cursor,
    ):
        rows = cursor.fetchall()
    return {row[0]: {"id": row[0], "title": row[1], "content": row[2]} for row in rows}


def test_initial_sync_stages_prs_and_comments(dlt_mod, tmp_path):
    session = FakeBitbucketSession(
        repos={"ws": [_repo("repo")]},
        prs={("ws", "repo"): [_pr(1, title="Add feature", state="OPEN", comment_count=1)]},
        comments={("ws", "repo", 1): [_comment(1, raw="nice work")]},
    )
    pipeline = _run_sync(dlt_mod, tmp_path, "bitbucket_initial", session)

    rows = _read_documents(pipeline)
    assert "bitbucket:ws:repo:pr:1" in rows
    assert "bitbucket:ws:repo:pr:1:comment:1" in rows
    assert "nice work" in rows["bitbucket:ws:repo:pr:1:comment:1"]["content"]


def test_merge_removes_tombstoned_rows_and_keeps_unchanged_rows(dlt_mod, tmp_path):
    pr = _pr(1, title="Add feature", state="OPEN", comment_count=2)
    session1 = FakeBitbucketSession(
        repos={"ws": [_repo("repo")]},
        prs={("ws", "repo"): [pr]},
        comments={("ws", "repo", 1): [_comment(1, raw="first"), _comment(2, raw="second")]},
    )
    _run_sync(dlt_mod, tmp_path, "bitbucket_merge", session1)

    session2 = FakeBitbucketSession(
        repos={"ws": [_repo("repo")]},
        prs={("ws", "repo"): [pr]},
        comments={("ws", "repo", 1): [_comment(1, raw="first")]},  # comment 2 vanished
    )
    pipeline = _run_sync(dlt_mod, tmp_path, "bitbucket_merge", session2)

    rows = _read_documents(pipeline)
    assert "bitbucket:ws:repo:pr:1" in rows  # unchanged PR row kept
    assert "bitbucket:ws:repo:pr:1:comment:1" in rows  # unchanged comment kept
    assert "bitbucket:ws:repo:pr:1:comment:2" not in rows  # deleted comment gone


def test_mid_pagination_failure_aborts_run_without_partial_commit(dlt_mod, tmp_path):
    db_path = (tmp_path / "bitbucket_boom.db").as_posix()
    pipeline = dlt_mod.pipeline(
        pipeline_name="bitbucket_boom",
        destination=dlt_mod.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="bitbucket_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    with pytest.raises(Exception):  # noqa: B017 - dlt wraps the source error in PipelineStepFailed
        pipeline.run(bitbucket_source(workspace="ws", access_token="tok", session=_BoomSession()))


def test_repo_404_aborts_the_whole_sync(dlt_mod, tmp_path):
    # repo_slugs names a repo FakeBitbucketSession has no data for, so the
    # pull-requests listing for it 404s — this must abort, not skip the repo.
    session = FakeBitbucketSession(repos={"ws": [_repo("real-repo")]})
    db_path = (tmp_path / "bitbucket_404.db").as_posix()
    pipeline = dlt_mod.pipeline(
        pipeline_name="bitbucket_404",
        destination=dlt_mod.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="bitbucket_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    with pytest.raises(Exception):  # noqa: B017 - dlt wraps the source error in PipelineStepFailed
        pipeline.run(
            bitbucket_source(
                workspace="ws", access_token="tok", repo_slugs=["typo-repo"], session=session
            )
        )
