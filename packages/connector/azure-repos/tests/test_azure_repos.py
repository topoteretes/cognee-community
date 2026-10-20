"""Unit tests for the Azure Repos connector, with the Azure DevOps API faked.

Covers listing every PR status, full descriptions, rendering (reviewers, work
items, commits, threads), fingerprint-based change detection including
comment-only edits, the recently-closed window, the three ways PRs leave memory,
the empty-listing guard, paging, the HTTP client, and the dlt wiring.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from cognee_community_connector_azure_repos import azure_repos as repos_mod
from cognee_community_connector_azure_repos.azure_repos import (
    AzureDevOpsAccessError,
    AzureDevOpsAuthError,
    AzureDevOpsClient,
    AzureDevOpsError,
    _Scope,
    azure_repos_source,
    render_pull_request,
    sync_pull_requests,
)

ORG_URL = "https://dev.azure.com/acme"
PROJECT = "Shop"
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


def repo(rid="r1", name="shop-api", **extra):
    return {"id": rid, "name": name, "webUrl": f"{ORG_URL}/Shop/_git/{name}", **extra}


def pull_request(pid, title="", *, status="active", closed=None, description="", **extra):
    return {
        "pullRequestId": pid,
        "title": title or f"PR {pid}",
        "status": status,
        "description": description,
        "closedDate": closed,
        "creationDate": "2026-10-01T10:00:00Z",
        "createdBy": {"displayName": "Priya Nair", "uniqueName": "priya@acme.com"},
        "sourceRefName": "refs/heads/feature/x",
        "targetRefName": "refs/heads/main",
        "lastMergeSourceCommit": {"commitId": "aaa"},
        "reviewers": [],
        **extra,
    }


def text_comment(content, author="Ravi Kumar", **extra):
    return {"content": content, "author": {"displayName": author}, "commentType": "text", **extra}


class FakeAzureRepos:
    """In-memory stand-in for the Git endpoints the connector calls."""

    def __init__(self, repos=(), page_cap=None):
        self.organization_url = ORG_URL
        self.repos = list(repos)
        self.prs: dict[str, list[dict]] = {}
        self.threads: dict[tuple, list[dict]] = {}
        self.commits: dict[tuple, list[dict]] = {}
        self.full_messages: dict[str, str] = {}
        self.work_items: dict[tuple, list[int]] = {}
        self.page_cap = page_cap
        self.calls: list[tuple[str, dict]] = []

    def add(self, repo_id, pr, threads=(), commits=(), work_items=()):
        self.prs.setdefault(repo_id, []).append(pr)
        self.threads[(repo_id, pr["pullRequestId"])] = list(threads)
        self.commits[(repo_id, pr["pullRequestId"])] = list(commits)
        self.work_items[(repo_id, pr["pullRequestId"])] = list(work_items)
        return pr

    def pr(self, repo_id, pid):
        return next(p for p in self.prs[repo_id] if p["pullRequestId"] == pid)

    def count(self, fragment):
        return sum(fragment in path for path, _ in self.calls)

    # -- client interface ---------------------------------------------------
    def get(self, path, params=None):
        params = params or {}
        self.calls.append((path, params))
        if path.endswith("/_apis/git/repositories"):
            return {"value": self.repos}
        repo_id = path.split("/repositories/")[1].split("/")[0]
        if path.endswith("/pullrequests"):
            assert params["searchCriteria.status"] == "all"
            listed = []
            for p in self.prs.get(repo_id, []):
                # The list endpoint truncates descriptions to 400 characters.
                listed.append({**p, "description": (p.get("description") or "")[:400]})
            top = min(params["$top"], self.page_cap or params["$top"])
            skip = params["$skip"]
            return {"value": listed[skip : skip + top]}
        if "/pullrequests/" in path:
            pid = int(path.rsplit("/", 1)[1])
            assert params.get("includeWorkItemRefs") == "true"
            refs = [{"id": str(w)} for w in self.work_items[(repo_id, pid)]]
            return {**self.pr(repo_id, pid), "workItemRefs": refs}
        if path.endswith("/threads"):
            pid = int(path.split("/pullRequests/")[1].split("/")[0])
            return {"value": self.threads[(repo_id, pid)]}
        if "/commits/" in path:
            sha = path.rsplit("/", 1)[1]
            return {"commitId": sha, "comment": self.full_messages[sha]}
        raise AssertionError(f"unexpected GET {path}")

    def get_page(self, path, params=None):
        params = params or {}
        self.calls.append((path, params))
        repo_id = path.split("/repositories/")[1].split("/")[0]
        pid = int(path.split("/pullRequests/")[1].split("/")[0])
        commits = self.commits[(repo_id, pid)]
        start = int(params.get("continuationToken") or 0)
        page = commits[start : start + 2]  # small pages to exercise the header token
        more = start + 2 < len(commits)
        return {"value": page}, (str(start + 2) if more else None)


def run(fake, state, now=NOW, **scope_kwargs):
    return list(sync_pull_requests(fake, _Scope(project=PROJECT, **scope_kwargs), state, now=now))


def keys(rows, deleted=False):
    return sorted(r["id"] for r in rows if r["_deleted"] is deleted)


# ---------------------------------------------------------------------------
# Listing and rendering
# ---------------------------------------------------------------------------
def test_first_sync_includes_every_status_with_full_descriptions():
    fake = FakeAzureRepos([repo()])
    long_text = "Why: " + "x" * 600
    fake.add("r1", pull_request(1, "Add retry", description=long_text))
    fake.add("r1", pull_request(2, status="completed", closed="2026-10-02T10:00:00Z"))
    fake.add("r1", pull_request(3, status="abandoned", closed="2026-10-03T10:00:00Z"))
    state: dict = {}

    rows = run(fake, state)

    assert keys(rows) == ["r1/1", "r1/2", "r1/3"]
    first = next(r for r in rows if r["id"] == "r1/1")
    assert first["title"] == "Pull request !1: Add retry"
    assert first["url"] == f"{ORG_URL}/Shop/_git/shop-api/pullrequest/1"
    assert long_text in first["content"]  # not the 400-character list version
    assert set(state["fingerprints"]) == {"r1/1", "r1/2", "r1/3"}


def test_render_has_reviewers_work_items_commits_and_threads_but_no_emails():
    pr = pull_request(
        45,
        "Retry card payments",
        status="completed",
        closed="2026-10-05T09:00:00Z",
        description="Card payments time out on Safari.",
        closedBy={"displayName": "Ravi Kumar"},
        labels=[{"name": "payments"}],
        workItemRefs=[{"id": "412"}, {"id": "398"}],
        reviewers=[
            {"displayName": "Ravi Kumar", "vote": 10},
            {"displayName": "Meera Shah", "vote": -5},
            {"displayName": "[Shop]\\Shop Team", "vote": 0, "isContainer": True},
        ],
    )
    threads = [
        {
            "id": 1,
            "status": "fixed",
            "threadContext": {"filePath": "/src/pay.py", "rightFileStart": {"line": 42}},
            "comments": [
                text_comment("Should this retry on 429 too?", "Meera Shah"),
                text_comment("Done, and it honours Retry-After.", "Priya Nair"),
                text_comment("old note", isDeleted=True),
            ],
        },
        {"id": 2, "comments": [{"content": "Priya voted 10", "commentType": "system"}]},
        {"id": 3, "status": "active", "comments": [text_comment("Ship it")]},
        {"id": 4, "isDeleted": True, "comments": [text_comment("gone")]},
    ]
    commits = [
        {
            "commitId": "1a2b3c4d5e",
            "comment": "Retry on timeout\n\nAlso logs the attempt.",
            "author": {"name": "Priya Nair", "email": "priya@acme.com"},
        },
    ]

    text = render_pull_request(repo(), pr, threads, commits)

    assert "Status: merged into main from feature/x" in text
    assert "Reviewers: Meera Shah (waiting for author), Ravi Kumar (approved)" in text
    assert "Shop Team" not in text
    assert "Work items: work item #398, work item #412" in text
    assert "Labels: payments" in text
    assert "- 1a2b3c4 Retry on timeout (Priya Nair)\n  Also logs the attempt." in text
    assert "On src/pay.py line 42 (resolved):" in text
    assert "- Meera Shah: Should this retry on 429 too?" in text
    assert "General (active):\n- Ravi Kumar: Ship it" in text
    for noise in ("voted 10", "old note", "gone", "@acme.com"):
        assert noise not in text


def test_open_and_draft_pr_status_lines():
    text = render_pull_request(repo(), pull_request(7, isDraft=True), [], [])
    assert "Status: open, feature/x into main (draft)" in text


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------
def test_second_run_with_no_changes_yields_nothing_and_skips_expensive_calls():
    fake = FakeAzureRepos([repo()])
    fake.add("r1", pull_request(1))
    state: dict = {}
    run(fake, state)
    fake.calls.clear()

    assert run(fake, state) == []
    assert fake.count("/commits") == 0  # only changed PRs get their commits read


def test_comment_only_change_on_an_open_pr_is_picked_up():
    fake = FakeAzureRepos([repo()])
    fake.add("r1", pull_request(1), threads=[])
    state: dict = {}
    run(fake, state)

    fake.threads[("r1", 1)].append(
        {"id": 9, "lastUpdatedDate": "2026-10-18T10:00:00Z", "comments": [text_comment("LGTM")]}
    )
    rows = run(fake, state)

    assert keys(rows) == ["r1/1"]
    assert "Ravi Kumar: LGTM" in rows[0]["content"]


def test_deleted_comment_on_an_open_pr_is_picked_up():
    thread = {
        "id": 9,
        "lastUpdatedDate": "2026-10-18T10:00:00Z",
        "comments": [text_comment("leaked password, oops")],
    }
    fake = FakeAzureRepos([repo()])
    fake.add("r1", pull_request(1), threads=[thread])
    state: dict = {}
    run(fake, state)

    fake.threads[("r1", 1)] = [
        {**thread, "isDeleted": True, "lastUpdatedDate": "2026-10-18T11:00:00Z"}
    ]
    rows = run(fake, state)

    assert keys(rows) == ["r1/1"]
    assert "leaked password" not in rows[0]["content"]


def test_description_edit_past_the_list_cutoff_is_picked_up_on_open_prs():
    fake = FakeAzureRepos([repo()])
    fake.add("r1", pull_request(1, description="Intro. " + "x" * 500 + " Rollout: Monday."))
    state: dict = {}
    run(fake, state)

    pr = fake.pr("r1", 1)
    pr["description"] = pr["description"].replace("Monday", "Friday")  # past char 400
    rows = run(fake, state)

    assert keys(rows) == ["r1/1"]
    assert "Rollout: Friday." in rows[0]["content"]


def test_work_item_linked_to_an_open_pr_is_picked_up():
    fake = FakeAzureRepos([repo()])
    fake.add("r1", pull_request(1))
    state: dict = {}
    run(fake, state)

    fake.work_items[("r1", 1)].append(412)
    rows = run(fake, state)

    assert "Work items: work item #412" in rows[0]["content"]


def test_renamed_repo_updates_its_prs_without_removing_them():
    fake = FakeAzureRepos([repo("r1", "shop-api")])
    fake.add("r1", pull_request(1, status="completed", closed="2026-01-05T10:00:00Z"))
    state: dict = {}
    run(fake, state)

    fake.repos = [repo("r1", "payments-api")]
    rows = run(fake, state)

    assert keys(rows) == ["r1/1"]
    assert "Repository: payments-api" in rows[0]["content"]


def test_old_closed_prs_skip_the_threads_call():
    fake = FakeAzureRepos([repo()])
    fake.add("r1", pull_request(1, status="completed", closed="2026-01-05T10:00:00Z"))
    state: dict = {}
    run(fake, state)
    fake.calls.clear()

    run(fake, state)

    assert fake.count("/threads") == 0
    assert fake.count("/pullrequests/1") == 0  # going quiet isn't a change


def test_comment_on_an_old_closed_pr_waits_unless_recheck_is_off():
    fake = FakeAzureRepos([repo()])
    fake.add("r1", pull_request(1, status="completed", closed="2026-01-05T10:00:00Z"), threads=[])
    state: dict = {}
    run(fake, state)
    fake.threads[("r1", 1)].append(
        {"id": 5, "lastUpdatedDate": "x", "comments": [text_comment("late")]}
    )

    assert run(fake, dict(state)) == []  # outside the 30-day window
    rows = run(fake, dict(state), recheck_closed_days=None)
    assert keys(rows) == ["r1/1"]


def test_recently_closed_pr_is_still_watched():
    fake = FakeAzureRepos([repo()])
    closed = (NOW - timedelta(days=3)).isoformat()
    fake.add("r1", pull_request(1, status="completed", closed=closed), threads=[])
    state: dict = {}
    run(fake, state)
    fake.threads[("r1", 1)].append(
        {"id": 5, "lastUpdatedDate": "x", "comments": [text_comment("post-merge note")]}
    )

    assert keys(run(fake, state)) == ["r1/1"]


def test_status_change_from_the_list_is_picked_up():
    fake = FakeAzureRepos([repo()])
    fake.add("r1", pull_request(1))
    state: dict = {}
    run(fake, state)

    fake.pr("r1", 1).update(status="completed", closedDate="2026-10-18T10:00:00Z")
    rows = run(fake, state)

    assert keys(rows) == ["r1/1"]
    assert "merged into main" in rows[0]["content"]


def test_new_vote_is_picked_up():
    fake = FakeAzureRepos([repo()])
    fake.add(
        "r1", pull_request(1, reviewers=[{"id": "u1", "displayName": "Ravi Kumar", "vote": 0}])
    )
    state: dict = {}
    run(fake, state)

    fake.pr("r1", 1)["reviewers"][0]["vote"] = 10
    rows = run(fake, state)

    assert "Ravi Kumar (approved)" in rows[0]["content"]


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------
def test_deleted_repo_tombstones_its_prs():
    fake = FakeAzureRepos([repo("r1", "shop-api"), repo("r2", "shop-web")])
    fake.add("r1", pull_request(1))
    fake.add("r2", pull_request(1))
    state: dict = {}
    run(fake, state)

    fake.repos = [repo("r1", "shop-api")]
    rows = run(fake, state)

    assert rows == [{"id": "r2/1", "_deleted": True}]
    assert set(state["fingerprints"]) == {"r1/1"}


def test_disabled_repo_keeps_its_prs():
    fake = FakeAzureRepos([repo()])
    fake.add("r1", pull_request(1))
    state: dict = {}
    run(fake, state)

    fake.repos = [repo(isDisabled=True)]
    assert run(fake, state) == []
    assert set(state["fingerprints"]) == {"r1/1"}


def test_empty_repo_listing_deletes_nothing():
    fake = FakeAzureRepos([repo()])
    fake.add("r1", pull_request(1))
    state: dict = {}
    run(fake, state)
    saved = dict(state)

    fake.repos = []
    assert run(fake, state) == []
    assert state == saved


def test_abandoned_prs_can_be_left_out():
    fake = FakeAzureRepos([repo()])
    fake.add("r1", pull_request(1))
    state: dict = {}
    run(fake, state, include_abandoned=False)

    fake.pr("r1", 1).update(status="abandoned", closedDate="2026-10-18T10:00:00Z")
    rows = run(fake, state, include_abandoned=False)

    assert rows == [{"id": "r1/1", "_deleted": True}]


def test_repository_filter_matches_names_case_insensitively():
    fake = FakeAzureRepos([repo("r1", "shop-api"), repo("r2", "shop-web")])
    fake.add("r1", pull_request(1))
    fake.add("r2", pull_request(2))

    assert keys(run(fake, {}, repositories=["Shop-API"])) == ["r1/1"]


def test_failed_fetch_raises_and_leaves_state_alone():
    fake = FakeAzureRepos([repo()])
    fake.add("r1", pull_request(1))
    state: dict = {}
    run(fake, state)
    saved = dict(state)

    fake.pr("r1", 1)["title"] = "changed"
    del fake.threads[("r1", 1)]  # the next threads call blows up
    with pytest.raises(KeyError):
        run(fake, state)
    assert state == saved


# ---------------------------------------------------------------------------
# Paging and commits
# ---------------------------------------------------------------------------
def test_pr_paging_survives_a_server_capping_page_size():
    fake = FakeAzureRepos([repo()], page_cap=3)
    for pid in range(1, 9):
        fake.add("r1", pull_request(pid))

    rows = run(fake, {}, include_commits=False)

    assert len(rows) == 8


def test_created_after_is_sent_as_a_created_time_filter():
    fake = FakeAzureRepos([repo()])
    run(fake, {}, created_after=date(2026, 1, 1))

    params = next(p for path, p in fake.calls if path.endswith("/pullrequests"))
    assert params["searchCriteria.queryTimeRangeType"] == "created"
    assert params["searchCriteria.minTime"] == "2026-01-01T00:00:00Z"


def test_commits_follow_the_continuation_header_and_untruncate_messages():
    fake = FakeAzureRepos([repo()])
    commits = [{"commitId": f"c{i}", "comment": f"commit {i}"} for i in range(5)]
    commits[3] = {"commitId": "c3", "comment": "short...", "commentTruncated": True}
    fake.full_messages["c3"] = "The whole message for commit 3"
    fake.add("r1", pull_request(1), commits=commits)

    rows = run(fake, {})

    assert all(f"c{i}" in rows[0]["content"] for i in range(5))
    assert "The whole message for commit 3" in rows[0]["content"]
    assert "short..." not in rows[0]["content"]


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------
class _Response:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers: dict = {}
        self.auth = None

    def request(self, method, url, **kwargs):
        return self.responses.pop(0)


@pytest.fixture
def clock(monkeypatch):
    now = {"t": 1000.0}
    sleeps: list[float] = []

    def sleep(seconds):
        sleeps.append(round(seconds, 3))
        now["t"] += seconds

    monkeypatch.setattr(repos_mod.time, "monotonic", lambda: now["t"])
    monkeypatch.setattr(repos_mod.time, "sleep", sleep)
    return sleeps


def test_client_returns_the_continuation_header(clock):
    session = _Session([_Response(200, {"value": [1]}, {"x-ms-continuationtoken": "abc"})])
    client = AzureDevOpsClient(ORG_URL, "pat", session=session)

    assert client.get_page("x") == ({"value": [1]}, "abc")
    assert session.auth == ("", "pat")


def test_client_retries_429_and_honours_retry_after_on_a_200(clock):
    session = _Session(
        [
            _Response(429, headers={"Retry-After": "3"}),
            _Response(200, {"n": 1}, {"Retry-After": "2"}),
            _Response(200, {"n": 2}),
        ]
    )
    client = AzureDevOpsClient(ORG_URL, "pat", session=session)

    assert client.get("a") == {"n": 1}
    assert client.get("b") == {"n": 2}
    assert clock == [3.0, 2.0]


def test_client_gives_up_after_repeated_server_errors(clock):
    session = _Session([_Response(503) for _ in range(repos_mod._MAX_ATTEMPTS)])
    with pytest.raises(RuntimeError, match="503"):
        AzureDevOpsClient(ORG_URL, "pat", session=session).get("x")
    assert clock == [1.0, 2.0, 4.0, 8.0]


# ---------------------------------------------------------------------------
# dlt wiring
# ---------------------------------------------------------------------------
def test_resource_is_a_scoped_merge_document_source():
    from cognee.tasks.ingestion.dlt_utils import PIPELINE_SCOPE_ATTR, document_source_tag

    resource = azure_repos_source(project=PROJECT, client=FakeAzureRepos())

    assert resource.name == "azure_repos_acme_shop"
    assert getattr(resource, PIPELINE_SCOPE_ATTR) == "azure_repos_acme_shop"
    assert document_source_tag(resource) == "azure_repos"
    schema = resource.compute_table_schema()
    disposition = schema.get("write_disposition")
    if isinstance(disposition, dict):
        disposition = disposition.get("disposition")
    assert disposition == "merge"
    assert schema["columns"]["id"].get("primary_key") is True
    assert schema["columns"]["_deleted"].get("hard_delete") is True


def test_source_needs_an_organization_and_a_token_argument(monkeypatch):
    # the token comes in as an argument only; the env var is the example's business
    monkeypatch.setenv("AZURE_DEVOPS_PAT", "from-env")
    with pytest.raises(ValueError, match="organization"):
        azure_repos_source(project=PROJECT, pat="x")
    with pytest.raises(ValueError, match="pat="):
        azure_repos_source(organization="acme", project=PROJECT)
    assert azure_repos_source(organization="acme", project=PROJECT, pat="x")


def test_check_active_can_stop_a_sync():
    fake = FakeAzureRepos([repo()])
    fake.add("r1", pull_request(1))

    def stop():
        raise PermissionError("access revoked")

    resource = azure_repos_source(project=PROJECT, client=fake, check_active=stop)
    with pytest.raises(Exception, match="access revoked"):
        list(resource)


def test_tombstones_remove_rows_in_a_real_dlt_merge(tmp_path):
    dlt = pytest.importorskip("dlt")
    pytest.importorskip("duckdb")

    fake = FakeAzureRepos([repo("r1", "shop-api"), repo("r2", "shop-web")])
    fake.add("r1", pull_request(1))
    fake.add("r2", pull_request(2))
    pipeline = dlt.pipeline(
        pipeline_name="test_azure_repos_e2e",
        pipelines_dir=str(tmp_path / "pipelines"),
        destination=dlt.destinations.duckdb(str(tmp_path / "repos.duckdb")),
        dataset_name="prs",
    )

    def table_ids():
        with pipeline.sql_client() as client:
            rows = client.execute_sql("SELECT id FROM azure_repos_acme_shop ORDER BY id")
        return [r[0] for r in rows]

    pipeline.run(azure_repos_source(project=PROJECT, client=fake))
    assert table_ids() == ["r1/1", "r2/2"]

    fake.repos = [repo("r1", "shop-api")]
    pipeline.run(azure_repos_source(project=PROJECT, client=fake))
    assert table_ids() == ["r1/1"]


def test_azure_devops_timestamps_with_seven_fraction_digits_parse():
    scope = _Scope(project=PROJECT)
    recent = {"status": "completed", "closedDate": "2026-10-17T16:30:02.0413914Z"}
    old = {"status": "completed", "closedDate": "2026-01-01T16:30:02.0413914Z"}

    assert scope.warm(recent, NOW) is True
    assert scope.warm(old, NOW) is False
    for stamp in ("2026-10-17T16:30:32.74Z", "2026-10-17T16:30:32Z", "2026-10-17T16:30:32.093Z"):
        assert scope.warm({"status": "completed", "closedDate": stamp}, NOW) is True


# ---------------------------------------------------------------------------
# Same hardening as the Boards connector got in review
# ---------------------------------------------------------------------------
def test_client_errors_keep_secrets_and_the_url_out_of_the_message(clock):
    session = _Session([_Response(401)])
    client = AzureDevOpsClient(ORG_URL, "super-secret-pat", session=session)

    with pytest.raises(AzureDevOpsAuthError) as err:
        client.get("Shop/_apis/git/repositories")
    assert "401" in str(err.value)
    assert "super-secret-pat" not in str(err.value) and "acme" not in str(err.value)
    assert err.value.url == f"{ORG_URL}/Shop/_apis/git/repositories"
    assert "super-secret-pat" not in repr(client)


def test_client_maps_403_and_caps_retry_after(clock):
    with pytest.raises(AzureDevOpsAccessError):
        AzureDevOpsClient(ORG_URL, "pat", session=_Session([_Response(403)])).get("x")
    session = _Session([_Response(429, headers={"Retry-After": "3600"}), _Response(200, {"ok": 1})])
    assert AzureDevOpsClient(ORG_URL, "pat", session=session).get("x") == {"ok": 1}
    assert clock == [60.0]


def test_client_retries_network_errors_then_raises(clock):
    import requests

    class _Down(_Session):
        def request(self, method, url, **kwargs):
            raise requests.ConnectionError(url)

    with pytest.raises(AzureDevOpsError) as err:
        AzureDevOpsClient(ORG_URL, "pat", session=_Down([])).get("Shop/x")
    assert "acme" not in str(err.value)
    assert clock == [1.0, 2.0, 4.0, 8.0]


def test_repeated_commit_token_raises_instead_of_looping():
    fake = FakeAzureRepos([repo()])
    fake.add(
        "r1", pull_request(1), commits=[{"commitId": f"c{i}", "comment": "x"} for i in range(6)]
    )
    fake.get_page = lambda path, params=None: (
        {"value": [{"commitId": "c", "comment": "x"}]},
        "same",
    )

    with pytest.raises(AzureDevOpsError, match="continuation token"):
        run(fake, {})


def test_pr_list_that_ignores_skip_raises_instead_of_looping():
    fake = FakeAzureRepos([repo()])
    fake.add("r1", pull_request(1))
    real_get = fake.get

    def ignores_skip(path, params=None):
        if path.endswith("/pullrequests"):
            params = {**params, "$skip": 0}
        return real_get(path, params)

    fake.get = ignores_skip
    with pytest.raises(AzureDevOpsError, match="same page"):
        run(fake, {})


def test_turning_commits_off_rerenders_every_pr_once():
    fake = FakeAzureRepos([repo()])
    fake.add(
        "r1",
        pull_request(1, status="completed", closed="2026-01-05T10:00:00Z"),
        commits=[{"commitId": "abc1234def", "comment": "Retry on timeout"}],
    )
    state: dict = {}
    assert "Retry on timeout" in run(fake, state)[0]["content"]

    rows = run(fake, state, include_commits=False)
    assert keys(rows) == ["r1/1"]
    assert "Retry on timeout" not in rows[0]["content"]
    assert run(fake, state, include_commits=False) == []


def test_budget_stops_cleanly_keeps_progress_and_deletes_nothing():
    fake = FakeAzureRepos([repo("r1", "shop-api"), repo("r2", "shop-web")])
    for pid in range(1, 6):
        fake.add("r1", pull_request(pid))
    fake.add("r2", pull_request(9))
    state: dict = {}
    run(fake, state, include_commits=False)

    fake.repos = [repo("r1", "shop-api")]  # r2 deleted
    for pid in range(1, 6):
        fake.pr("r1", pid)["title"] = "changed"
    stats: dict = {}
    first = list(
        sync_pull_requests(
            fake,
            _Scope(project=PROJECT, include_commits=False),
            state,
            stats,
            now=NOW,
            max_requests=6,
        )
    )

    assert stats["failed_budget"] == 1
    assert not any(r["_deleted"] for r in first)  # incomplete listing: no deletes
    rest = run(fake, state, include_commits=False)
    assert sorted(r["id"] for r in first + rest if not r["_deleted"]) == [
        f"r1/{i}" for i in range(1, 6)
    ]
    assert [r["id"] for r in rest if r["_deleted"]] == ["r2/9"]


def test_rate_limit_that_wont_clear_stops_cleanly():
    fake = FakeAzureRepos([repo()])
    fake.add("r1", pull_request(1))

    def limited(*args, **kwargs):
        raise repos_mod.AzureDevOpsRateLimitedError("Azure DevOps returned HTTP 429", status=429)

    fake.get = limited
    stats: dict = {}
    assert list(sync_pull_requests(fake, _Scope(project=PROJECT), {}, stats, now=NOW)) == []
    assert stats["failed_rate_limit"] == 1


def test_check_active_runs_before_every_request():
    fake = FakeAzureRepos([repo()])
    fake.add("r1", pull_request(1), commits=[{"commitId": "c1", "comment": "x"}])
    checks = []

    list(
        sync_pull_requests(
            fake, _Scope(project=PROJECT), {}, now=NOW, check_active=lambda: checks.append(1)
        )
    )

    assert len(checks) == len(fake.calls)


def test_shuffled_threads_and_commits_render_the_same_text():
    import random

    threads = [
        {"id": n, "comments": [text_comment(f"note {n}", publishedDate=f"2026-10-0{n}")]}
        for n in (3, 1, 2)
    ]
    commits = [
        {
            "commitId": f"c{n}",
            "comment": f"commit {n}",
            "author": {"name": "P", "date": f"2026-10-0{n}"},
        }
        for n in (2, 3, 1)
    ]
    pr = pull_request(1)
    first = render_pull_request(repo(), pr, threads, commits)

    assert (
        render_pull_request(repo(), pr, random.sample(threads, 3), random.sample(commits, 3))
        == first
    )


def test_resource_name_version_guard_and_stats(monkeypatch):
    from cognee.tasks.ingestion.dlt_utils import PIPELINE_SCOPE_ATTR

    fake = FakeAzureRepos([repo()])
    fake.add("r1", pull_request(1))
    fake.add("r1", pull_request(2, status="abandoned", closed="2026-10-01T10:00:00Z"))
    resource = azure_repos_source(
        project=PROJECT, service=fake, resource_name="shop_prs", include_abandoned=False
    )
    list(resource)

    assert resource.name == "shop_prs" and getattr(resource, PIPELINE_SCOPE_ATTR) == "shop_prs"
    assert resource.cognee_sync_stats == {"scanned": 1, "skipped": 0, "deleted": 0, "failed": 0}

    monkeypatch.setattr(repos_mod.dlt_utils, "DOCUMENT_SYNC_VERSION", 0)
    with pytest.raises(RuntimeError, match=r"1\.6\.3"):
        azure_repos_source(project=PROJECT, service=fake)
