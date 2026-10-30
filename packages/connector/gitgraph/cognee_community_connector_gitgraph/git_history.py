"""Turn a git repo's history into plain rows: one per commit and one per file change."""

from pathlib import Path
from typing import Any

from git import GitCommandError, InvalidGitRepositoryError, NoSuchPathError, Repo
from gitdb.exc import BadName

_CHANGE_TYPES = {"A": "A", "M": "M", "D": "D", "R": "R", "C": "M", "T": "M"}


def _open(repo_path: str | Path) -> Repo:
    try:
        return Repo(repo_path)
    except (InvalidGitRepositoryError, NoSuchPathError) as exc:
        raise ValueError(f"not a git repository: {repo_path}") from exc


def _file_changes(repo: Repo, sha: str) -> list[dict[str, Any]]:
    # ponytail: two git processes per commit; fine for demo-sized repos, batch with
    # `git log --numstat` if large repos get slow.
    args = ["-r", "-M", "--root", "--no-commit-id", "-z"]
    status = repo.git.diff_tree(*args, "--name-status", sha).split("\0")
    numstat = repo.git.diff_tree(*args, "--numstat", sha).split("\0")
    counts: dict[str, tuple[int, int]] = {}
    i = 0
    while i < len(numstat) and numstat[i]:
        added, deleted, path = numstat[i].split("\t", 2)
        i += 1
        if not path:  # rename: the old and new paths follow as separate fields
            path = numstat[i + 1]
            i += 2
        counts[path] = (int(added) if added != "-" else 0, int(deleted) if deleted != "-" else 0)
    rows: list[dict[str, Any]] = []
    i = 0
    while i < len(status) and status[i]:
        code = status[i][0]
        old_path = None
        if code in "RC":
            old_path, path = status[i + 1], status[i + 2]
            i += 3
        else:
            path = status[i + 1]
            i += 2
        added_lines, deleted_lines = counts.get(path, (0, 0))
        rows.append(
            {
                "sha": sha,
                "path": path,
                "change_type": _CHANGE_TYPES[code],
                "lines_added": added_lines,
                "lines_deleted": deleted_lines,
                "old_path": old_path if code == "R" else None,
            }
        )
    return sorted(rows, key=lambda r: r["path"])


def extract_commits(
    repo_path: str | Path, refs: list[str] | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Return ``{"commits": [...], "file_changes": [...]}`` for every commit reachable from refs.

    ``refs`` are branch names; ``None`` means all local branches. Each commit appears once and
    lists, in ``refs``, the given branches it is reachable from. Commits are ordered by authored
    time (UTC epoch, then sha). Merge commits yield no ``file_changes`` rows (``diff-tree`` runs
    without ``-m``).
    """
    repo = _open(repo_path)
    names = sorted(set(refs)) if refs is not None else sorted(h.name for h in repo.heads)
    commits: dict[str, dict[str, Any]] = {}
    epochs: dict[str, int] = {}
    for name in names:
        try:
            tip = repo.commit(name)
        except (BadName, ValueError, GitCommandError) as exc:
            raise ValueError(f"unknown ref: {name}") from exc
        for commit in repo.iter_commits(tip):
            row = commits.get(commit.hexsha)
            if row is None:
                message = commit.message
                row = {
                    "sha": commit.hexsha,
                    "parents": [p.hexsha for p in commit.parents],
                    "author": commit.author.name,
                    "author_email": commit.author.email,
                    "date": commit.authored_datetime.isoformat(),
                    "message": (message if isinstance(message, str) else message.decode()).strip(),
                    "refs": [],
                }
                commits[commit.hexsha] = row
                epochs[commit.hexsha] = commit.authored_date
            row["refs"].append(name)
    ordered = sorted(commits.values(), key=lambda c: (epochs[c["sha"]], c["sha"]))
    file_changes = [row for c in ordered for row in _file_changes(repo, c["sha"])]
    return {"commits": ordered, "file_changes": file_changes}
