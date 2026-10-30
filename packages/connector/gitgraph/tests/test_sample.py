"""The sample monorepo must be deterministic, well-formed and safe to build."""

import ast
from pathlib import Path

import pytest
from git import Repo

from cognee_community_connector_gitgraph import SampleRepo, build_sample_repo

MAIN_SHA = "720356a25b41bf9d10c90725ba3e9422b2acf2ec"
FEATURE_SHA = "b73e5edafa07274b11e3471c632d4d1300734dab"


@pytest.fixture
def sample(tmp_path: Path) -> SampleRepo:
    return build_sample_repo(tmp_path / "repo")


def _py_files(repo: Repo, branch: str) -> dict[str, str]:
    paths = repo.git.ls_tree("-r", "--name-only", branch).splitlines()
    return {p: repo.git.show(f"{branch}:{p}") for p in paths if p.endswith(".py")}


def test_shas_are_deterministic(tmp_path: Path) -> None:
    a = Repo(build_sample_repo(tmp_path / "a").path)
    b = Repo(build_sample_repo(tmp_path / "b").path)
    for branch in ("main", "feature/rotate-tokens"):
        assert a.git.rev_parse(branch) == b.git.rev_parse(branch)


def test_head_shas_are_pinned(sample: SampleRepo) -> None:
    repo = Repo(sample.path)
    assert repo.git.rev_parse("main") == MAIN_SHA
    assert repo.git.rev_parse("feature/rotate-tokens") == FEATURE_SHA


def test_branches_and_history(sample: SampleRepo) -> None:
    repo = Repo(sample.path)
    assert sample.base == "main"
    assert sample.head == "feature/rotate-tokens"
    assert {h.name for h in repo.heads} == {"main", "feature/rotate-tokens"}
    assert int(repo.git.rev_list("--count", "main")) == 7
    assert int(repo.git.rev_list("--count", "main..feature/rotate-tokens")) == 1


def test_feature_touches_only_auth(sample: SampleRepo) -> None:
    repo = Repo(sample.path)
    names = repo.git.diff("--name-only", "main", "feature/rotate-tokens").splitlines()
    assert names == ["shared/auth.py"]


def test_a_main_commit_cochanges_auth_and_invoice(sample: SampleRepo) -> None:
    repo = Repo(sample.path)
    assert any(
        {"shared/auth.py", "billing/invoice.py"} <= set(c.stats.files)
        for c in repo.iter_commits("main")
    )


@pytest.mark.parametrize("branch", ["main", "feature/rotate-tokens"])
def test_every_python_file_parses(sample: SampleRepo, branch: str) -> None:
    files = _py_files(Repo(sample.path), branch)
    assert "shared/auth.py" in files
    for path, source in files.items():
        ast.parse(source, filename=path)


def test_search_is_isolated_and_receipts_import_checkout(sample: SampleRepo) -> None:
    files = _py_files(Repo(sample.path), "main")
    search = {p: s for p, s in files.items() if p.startswith("search/")}
    assert search
    assert not any("shared" in s or "auth" in s for s in search.values())
    assert "from orders.checkout import" in files["notifications/receipts.py"]


def test_working_tree_is_clean_on_main(sample: SampleRepo) -> None:
    repo = Repo(sample.path)
    assert repo.active_branch.name == "main"
    assert not repo.is_dirty(untracked_files=True)


def test_refuses_existing_repo(tmp_path: Path) -> None:
    Repo.init(tmp_path)
    with pytest.raises(FileExistsError):
        build_sample_repo(tmp_path)


def test_refuses_non_empty_dir(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("keep me\n")
    with pytest.raises(FileExistsError):
        build_sample_repo(tmp_path)
    assert (tmp_path / "notes.txt").read_text() == "keep me\n"


def test_accepts_empty_existing_dir(tmp_path: Path) -> None:
    assert build_sample_repo(tmp_path).path == tmp_path
