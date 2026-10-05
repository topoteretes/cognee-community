"""Tests for GitHub dlt connector — no live API calls.

Two layers:
(a) DB-free: row builders + markdown rendering via fake dicts.
(b) dlt pipeline: injectable fake httpx client → temp sqlite,
    proves re-sync edits + forget-on-delete (replace snapshot).
"""

import pytest
import dlt
from cognee_community_connector_github.github import (
    _repo_to_row, _issue_to_row, _pr_to_row, _commit_to_row, _release_to_row,
)

# ---------------------------------------------------------------------------
# (a) Unit layer — fake dicts
# ---------------------------------------------------------------------------

def test_repo_row_shape_stable_id():
    repo = {"full_name":"o/r","name":"r","html_url":"https://gh/o/r","description":"hello"}
    row = _repo_to_row(repo)
    assert row == {"id":"repo:o/r","url":"https://gh/o/r","title":"o/r","content":"hello"}

def test_issue_row_shape_stable_id():
    issue = {"number":42,"title":"Bug","body":"fix","html_url":"https://gh/o/r/i/42"}
    row = _issue_to_row(issue,"o/r")
    assert row["id"] == "issue:o/r#42"
    assert row["title"] == "Bug"
    assert row["content"] == "fix"

def test_pr_row_shape_stable_id():
    pr = {"number":7,"title":"Feat","body":"new","html_url":"https://gh/o/r/p/7","review_comment_count":3}
    row = _pr_to_row(pr,"o/r")
    assert row["id"] == "pr:o/r#7"
    assert "review_comments: 3" in row["content"]

def test_commit_row_shape_stable_id():
    c = {"sha":"abc","commit":{"message":"m"},"files":[{"filename":"a.py"}],"html_url":"https://gh/o/r/c/abc"}
    row = _commit_to_row(c,"o/r")
    assert row["id"] == "commit:o/r#abc"
    assert row["content"] == "m\n\nchanged files:\na.py"

def test_release_row_shape_stable_id():
    r = {"tag_name":"v1","name":"v1","body":"notes","html_url":"https://gh/o/r/r/v1"}
    row = _release_to_row(r,"o/r")
    assert row["id"] == "release:o/r#v1"
    assert "# v1" in row["content"]

# ---------------------------------------------------------------------------
# (b) dlt pipeline layer — injectable fake client → temp sqlite
# ---------------------------------------------------------------------------

class FakeResp:
    def __init__(self, data):
        self.status_code = 200; self.headers = {"content-type":"application/json","link":""}
        self._data = data
    def json(self): return self._data
    def text(self): return str(self._data)
    def raise_for_status(self):
        if self.status_code >= 400: raise Exception(str(self.status_code))

class FakeClient:
    def __init__(self, data):
        self.base_url = "https://api.github.com"
        self._data = data
    def request(self, method, url, params=None, timeout=30.0, **kwargs):
        path = url.replace(self.base_url, "")
        payload = self._data.get(path, [])
        return FakeResp(payload)

def test_pipeline_sync_edit_and_delete(tmp_path, monkeypatch):
    from cognee_community_connector_github.github import github_source
    data = {
        "/repos/o/test":{"full_name":"o/test","name":"test","description":"d"},
        "/repos/o/test/issues":[{"number":1,"title":"One","body":"v1","pull_request":None}],
        "/repos/o/test/pulls":[],
        "/repos/o/test/commits":[{"sha":"a1","commit":{"message":"m"},"files":[]}],
        "/repos/o/test/releases":[],
    }
    fake = FakeClient(data)
    monkeypatch.setattr("httpx.Client", lambda **kw: fake)
    db_path = (tmp_path/"github.db").as_posix()
    pl = dlt.pipeline(
        pipeline_name="gh_test", destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="gh", pipelines_dir=str(tmp_path/"state"),
    )
    pl.run(github_source(token="fake", repos=["o/test"], client=fake))
    # edit: change body
    data["/repos/o/test/issues"][0]["body"] = "v2"
    pl.run(github_source(token="fake", repos=["o/test"], client=fake))
    # delete: empty issues (simulating vanished)
    data["/repos/o/test/issues"] = []
    pl.run(github_source(token="fake", repos=["o/test"], client=fake))
    with pl.sql_client() as c:
        with c.execute_query("SELECT id FROM github_documents") as cur:
            ids = [r[0] for r in cur.fetchall()]
    assert "issue:o/test#1" not in ids  # deleted/vanished drops out
    assert "repo:o/test" in ids
