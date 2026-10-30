"""Git history rows: commits and per-file changes, deduped across branches."""

from datetime import UTC, datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path

import pytest
from git import Repo

from cognee_community_connector_gitgraph import SampleRepo, build_sample_repo
from cognee_community_connector_gitgraph.git_history import extract_commits

MAIN_SHA = "720356a25b41bf9d10c90725ba3e9422b2acf2ec"  # pinned, same as test_sample.py
FEATURE_SHA = "b73e5edafa07274b11e3471c632d4d1300734dab"
FEATURE = "feature/rotate-tokens"


@pytest.fixture
def sample(tmp_path: Path) -> SampleRepo:
    return build_sample_repo(tmp_path / "repo")


def test_all_branches_and_single_ref(sample: SampleRepo) -> None:
    assert len(extract_commits(sample.path)["commits"]) == 8
    assert len(extract_commits(sample.path, ["main"])["commits"]) == 7


def test_parent_edges_and_pinned_heads(sample: SampleRepo) -> None:
    commits = extract_commits(sample.path)["commits"]
    by_sha = {c["sha"]: c for c in commits}
    main = [c for c in commits if "main" in c["refs"]]
    assert main[0]["parents"] == []
    for prev, cur in pairwise(main):
        assert cur["parents"] == [prev["sha"]]
    assert main[-1]["sha"] == MAIN_SHA
    assert by_sha[FEATURE_SHA]["parents"] == [MAIN_SHA]
    assert by_sha[FEATURE_SHA]["refs"] == [FEATURE]
    assert by_sha[MAIN_SHA]["refs"] == [FEATURE, "main"]


def test_commit_fields(sample: SampleRepo) -> None:
    head = {c["sha"]: c for c in extract_commits(sample.path)["commits"]}[FEATURE_SHA]
    assert head["author"] == "Ada Dev"
    assert head["author_email"] == "ada@example.com"
    assert head["date"] == "2024-01-08T12:00:00+00:00"
    assert head["message"] == "rotate token keys"


def test_feature_file_changes(sample: SampleRepo) -> None:
    rows = [r for r in extract_commits(sample.path)["file_changes"] if r["sha"] == FEATURE_SHA]
    assert len(rows) == 1
    row = rows[0]
    assert (row["path"], row["change_type"], row["old_path"]) == ("shared/auth.py", "M", None)
    assert row["lines_added"] > 0
    assert row["lines_deleted"] > 0


def test_token_expiry_cochanges_auth_and_invoice(sample: SampleRepo) -> None:
    out = extract_commits(sample.path)
    sha = next(c["sha"] for c in out["commits"] if c["message"] == "add token expiry")
    rows = [r for r in out["file_changes"] if r["sha"] == sha]
    assert [r["path"] for r in rows] == ["billing/invoice.py", "shared/auth.py"]
    assert {r["change_type"] for r in rows} == {"M"}


def test_root_commit_adds_files(sample: SampleRepo) -> None:
    out = extract_commits(sample.path)
    root = out["commits"][0]["sha"]
    rows = [r for r in out["file_changes"] if r["sha"] == root]
    assert [r["path"] for r in rows] == ["shared/__init__.py", "shared/auth.py"]
    assert {r["change_type"] for r in rows} == {"A"}


def test_rename_row(tmp_path: Path) -> None:
    repo = Repo.init(tmp_path, initial_branch="main")
    body = "".join(f"line {i}\n" for i in range(20))
    (tmp_path / "a.txt").write_text(body)
    repo.index.add(["a.txt"])
    repo.index.commit("one")
    repo.index.move(["a.txt", "b.txt"])
    repo.index.commit("two")
    rows = extract_commits(tmp_path)["file_changes"]
    renamed = [r for r in rows if r["change_type"] == "R"]
    assert len(renamed) == 1
    assert (renamed[0]["path"], renamed[0]["old_path"]) == ("b.txt", "a.txt")


def test_order_is_chronological_across_offsets(tmp_path: Path) -> None:
    repo = Repo.init(tmp_path, initial_branch="main")
    (tmp_path / "a.txt").write_text("a\n")
    repo.index.add(["a.txt"])
    # 08:00 UTC, but its ISO string sorts before the earlier 05:00 UTC commit's string
    # (aware datetimes: GitPython ignores the offset in ISO date strings)
    t_later = datetime(2024, 1, 1, 8, tzinfo=UTC)
    t_earlier = datetime(2024, 1, 1, 10, tzinfo=timezone(timedelta(hours=5)))
    later = repo.index.commit("later", author_date=t_later, commit_date=t_later)
    earlier = repo.index.commit(
        "earlier", parent_commits=[], head=False, author_date=t_earlier, commit_date=t_earlier
    )
    repo.create_head("other", earlier)
    commits = extract_commits(tmp_path)["commits"]
    assert [c["sha"] for c in commits] == [earlier.hexsha, later.hexsha]


def test_integrity(sample: SampleRepo) -> None:
    out = extract_commits(sample.path)
    shas = [c["sha"] for c in out["commits"]]
    assert len(set(shas)) == len(shas)
    keys = [(r["sha"], r["path"]) for r in out["file_changes"]]
    assert len(set(keys)) == len(keys)
    assert {r["sha"] for r in out["file_changes"]} <= set(shas)
    assert all(
        set(r) == {"sha", "path", "change_type", "lines_added", "lines_deleted", "old_path"}
        for r in out["file_changes"]
    )


def test_deterministic_and_ref_order_independent(sample: SampleRepo) -> None:
    first = extract_commits(sample.path)
    assert extract_commits(sample.path) == first
    assert extract_commits(sample.path, ["main", FEATURE]) == extract_commits(
        sample.path, [FEATURE, "main"]
    )


def test_unknown_ref_raises(sample: SampleRepo) -> None:
    with pytest.raises(ValueError, match="nope"):
        extract_commits(sample.path, ["nope"])


def test_non_repo_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        extract_commits(tmp_path)
