"""Unit tests for the git history dlt connector.

Two layers, runnable without any live service (everything is a real local
Git repo built in a temp directory — git itself is the only dependency):

* Pure-function tests for commit rendering and trailer extraction.
* git-plumbing tests against real temp repos, covering the acceptance-style
  behaviours: incremental sync picks up only new commits, a rebase/rewrite
  tombstones the abandoned commits, a shallow clone is refused, and a merge
  commit / root commit / non-UTF-8 message are handled.
"""

import subprocess
from pathlib import Path

import pytest

from cognee_community_connector_githistory.githistory import (
    GitHistoryError,
    _Commit,
    _commit_to_row,
    _extract_trailers,
    _is_shallow,
    _looks_like_bot,
    _reachable_shas,
    _require_git_repo,
    githistory_source,
)

# ---------------------------------------------------------------------------
# Pure-function tests
# ---------------------------------------------------------------------------


def test_extract_trailers_basic():
    body = "Fix the retry bug.\n\nFixes: #123\nCo-authored-by: Jane <jane@example.com>"
    assert _extract_trailers(body) == [
        ("Fixes", "#123"),
        ("Co-authored-by", "Jane <jane@example.com>"),
    ]


def test_extract_trailers_none_when_last_block_is_prose():
    body = "Fix the retry bug.\n\nThis took a while to track down."
    assert _extract_trailers(body) == []


def test_extract_trailers_empty_body():
    assert _extract_trailers("") == []


def test_looks_like_bot():
    assert _looks_like_bot("dependabot[bot]", "support@github.com")
    assert _looks_like_bot("github-actions[bot]", "actions@github.com")
    assert _looks_like_bot("Jane Doe", "jane@example.com") is False


def test_commit_to_row_marks_merge_and_includes_trailers():
    commit = _Commit(
        sha="abc123",
        author_name="Jane Doe",
        author_email="jane@example.com",
        author_date="2026-01-01T00:00:00+00:00",
        subject="Retry payment client 3x",
        body="Fixes a flaky gateway.\n\nFixes: #42",
        parents=["p1", "p2"],
        files=[("src/client.py", 10, 2)],
        refs=["HEAD -> main"],
    )
    row = _commit_to_row(commit, include_diffs=False)
    assert row["sha"] == "abc123"
    assert row["is_merge"] is True
    assert row["trailers"] == {"Fixes": "#42"}
    assert "Retry payment client 3x" in row["text"]
    assert "src/client.py" in row["text"]
    assert "Merge commit" in row["text"]


def test_commit_to_row_root_commit_not_marked_merge():
    commit = _Commit(
        sha="root1",
        author_name="Jane Doe",
        author_email="jane@example.com",
        author_date="2026-01-01T00:00:00+00:00",
        subject="Initial commit",
        body="",
        parents=[],
        files=[("README.md", 5, 0)],
    )
    row = _commit_to_row(commit, include_diffs=False)
    assert row["is_merge"] is False


# ---------------------------------------------------------------------------
# Real-repo fixtures
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str, env: dict | None = None) -> str:
    full_env = {
        "GIT_AUTHOR_NAME": "Test Author",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test Author",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    if env:
        full_env.update(env)
    import os

    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env={**os.environ, **full_env},
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / "a.txt").write_text("one\n")
    _git(r, "add", "a.txt")
    _git(r, "commit", "-q", "-m", "Initial commit\n\nStarts the project.")
    return r


def test_require_git_repo_rejects_non_repo(tmp_path: Path):
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    with pytest.raises(GitHistoryError):
        _require_git_repo(not_a_repo, "git")


def test_is_shallow_false_for_normal_clone(repo: Path):
    assert _is_shallow(repo, "git") is False


def test_shallow_clone_is_refused(tmp_path: Path, repo: Path):
    for i in range(3):
        (repo / "a.txt").write_text(f"line {i}\n")
        _git(repo, "commit", "-aq", "-m", f"Change {i}")

    clone = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "--depth", "1", f"file://{repo}", str(clone)],
        check=True,
        capture_output=True,
    )
    with pytest.raises(GitHistoryError, match="shallow"):
        githistory_source(str(clone))


def test_reachable_shas_lists_all_commits(repo: Path):
    (repo / "a.txt").write_text("two\n")
    _git(repo, "commit", "-aq", "-m", "Second commit")
    shas = _reachable_shas(repo, ["HEAD"], None, "git")
    assert len(shas) == 2


def test_incremental_sync_only_yields_new_commits(repo: Path):
    import dlt

    source = githistory_source(str(repo))
    pipeline = dlt.pipeline(
        pipeline_name="test_githistory",
        destination="duckdb",
        dataset_name="test",
        full_refresh=True,
    )
    info1 = pipeline.run(source)
    assert info1

    with pipeline.sql_client() as client:
        rows = client.execute_sql("SELECT sha FROM git_commits")
        assert len(rows) == 1

    (repo / "a.txt").write_text("two\n")
    _git(repo, "commit", "-aq", "-m", "Second commit")

    source2 = githistory_source(str(repo))
    pipeline.run(source2)

    with pipeline.sql_client() as client:
        rows = client.execute_sql("SELECT sha FROM git_commits")
        assert len(rows) == 2


def test_rebase_tombstones_abandoned_commit(repo: Path):
    import dlt

    (repo / "a.txt").write_text("two\n")
    _git(repo, "commit", "-aq", "-m", "Second commit")
    second_sha = _git(repo, "rev-parse", "HEAD").strip()

    pipeline = dlt.pipeline(
        pipeline_name="test_githistory_rebase",
        destination="duckdb",
        dataset_name="test_rebase",
        full_refresh=True,
    )
    pipeline.run(githistory_source(str(repo)))

    with pipeline.sql_client() as client:
        shas = {row[0] for row in client.execute_sql("SELECT sha FROM git_commits")}
        assert second_sha in shas

    # Simulate a history rewrite: reset to the first commit and make a new
    # second commit, abandoning the old one.
    _git(repo, "reset", "--hard", "HEAD~1")
    (repo / "a.txt").write_text("two-rewritten\n")
    _git(repo, "commit", "-aq", "-m", "Second commit, rewritten")

    pipeline.run(githistory_source(str(repo)))

    with pipeline.sql_client() as client:
        shas = {row[0] for row in client.execute_sql("SELECT sha FROM git_commits")}
        # merge write_disposition applies the hard-delete: the abandoned SHA
        # should no longer be present in the final table.
        assert second_sha not in shas


def test_root_commit_has_no_parents(repo: Path):
    from cognee_community_connector_githistory.githistory import _show_commit

    sha = _reachable_shas(repo, ["HEAD"], None, "git")[0]
    commit = _show_commit(
        repo, sha, None, include_diffs=False, diff_byte_cap=1000, git_binary="git"
    )
    assert commit.parents == []


def test_merge_commit_has_two_parents(repo: Path):
    from cognee_community_connector_githistory.githistory import _show_commit

    _git(repo, "checkout", "-qb", "feature")
    (repo / "b.txt").write_text("feature\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "Feature commit")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "--no-ff", "-q", "-m", "Merge feature", "feature")

    merge_sha = _git(repo, "rev-parse", "HEAD").strip()
    commit = _show_commit(
        repo, merge_sha, None, include_diffs=False, diff_byte_cap=1000, git_binary="git"
    )
    assert len(commit.parents) == 2


def test_non_utf8_commit_message_does_not_crash(repo: Path):
    from cognee_community_connector_githistory.githistory import _show_commit

    (repo / "a.txt").write_text("three\n")
    _git(repo, "add", "a.txt")
    # Non-UTF-8 byte in the message; git stores commit messages as raw bytes.
    _git(
        repo,
        "commit",
        "-q",
        "-m",
        "Weird char: caf\u00e9",
    )
    sha = _git(repo, "rev-parse", "HEAD").strip()
    commit = _show_commit(
        repo, sha, None, include_diffs=False, diff_byte_cap=1000, git_binary="git"
    )
    assert commit is not None
    assert "Weird char" in commit.subject


def test_diff_over_cap_is_truncated(repo: Path):
    from cognee_community_connector_githistory.githistory import _diff_text

    big_content = "x" * 5000 + "\n"
    (repo / "a.txt").write_text(big_content * 5)
    _git(repo, "commit", "-aq", "-m", "Big change")
    sha = _git(repo, "rev-parse", "HEAD").strip()
    text = _diff_text(repo, sha, parents=["dummy"], paths=None, byte_cap=500, git_binary="git")
    assert len(text.encode("utf-8")) < 2000
    assert "truncated" in text
