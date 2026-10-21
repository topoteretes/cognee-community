"""DLT source for local Git repository commit history (incremental + forget-on-delete).

Reads commit history directly from a local Git clone with the ``git`` binary —
no network calls, no API token, no rate limit — and yields one row per commit
as a dlt resource for cognee's ingestion pipeline.

Like the Notion connector, commits are ingested as *normal documents*: the
source declares ``cognee_document_source = "githistory"``, so
``resolve_dlt_sources`` tags each row ``external_metadata["source"] = "githistory"``
(not ``"dlt"``). This routes commit messages through the standard cognify
entity-extraction pipeline, which is the right treatment for the prose inside
a commit message (the "why", not just the "what").

Sync model
----------

A commit SHA is permanent once created — the same SHA is never rewritten, only
abandoned. That makes this an **incremental, SHA-keyed merge sync** rather
than the full-snapshot model used by Notion/Slack:

* ``git rev-list <refs>`` lists every commit SHA currently reachable from the
  configured refs. This is cheap (no commit bodies) even on large histories.
* SHAs already present in dlt resource state are skipped; only new commits are
  rendered and yielded (``write_disposition="merge"``).
* SHAs that *were* ingested but are no longer reachable (rebase, force-push,
  ``filter-repo``/``filter-branch`` history rewrite, branch deletion) are
  emitted as ``_deleted`` hard-delete tombstones, so cognee's ingestion layer
  removes them from the graph and vector stores on the next sync. This is the
  main reason history rewriting — e.g. scrubbing a leaked credential out of a
  repo — must actually remove the commit from memory, not just hide it.

Safety rule: the reachable-SHA listing (``git rev-list``) must complete
successfully *in full* before any tombstone is emitted. A git failure partway
through (corrupt repo, missing object) aborts the run with nothing deleted —
a partial reachable-set must never be mistaken for "everything else was
removed". A **shallow clone** is refused for the same reason: its reachable
history is truncated by design, so every commit before the shallow boundary
would look deleted on the very first run.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("githistory_connector")

GITHISTORY_TABLE_NAME = "git_commits"
GITHISTORY_SOURCE_NAME = "githistory"

# NUL-separated field delimiter and record delimiter: commit messages can
# contain any byte except NUL, so these are the only safe separators for
# `git log --format`.
_FIELD_SEP = "\x1f"
_RECORD_SEP = "\x1e"

# Default size cap for an included diff (bytes of combined patch text), to
# keep a single huge commit (a vendored-dependency bump, a generated-file
# commit) from dwarfing every other document in the dataset.
_DEFAULT_DIFF_CAP = 20_000

_EXTRA_HINT = "The git history connector requires a local `git` binary on PATH."

_BOT_AUTHOR_HINTS = (
    "dependabot[bot]",
    "renovate[bot]",
    "github-actions[bot]",
    "[bot]",
)


class GitHistoryError(RuntimeError):
    """Raised for a configuration or repository problem the caller must fix."""


@dataclass
class _Commit:
    sha: str
    author_name: str
    author_email: str
    author_date: str
    subject: str
    body: str
    parents: list[str]
    files: list[tuple[str, int, int]] = field(default_factory=list)  # (path, +, -)
    refs: list[str] = field(default_factory=list)


def githistory_source(
    repo_path: str,
    refs: list[str] | None = None,
    paths: list[str] | None = None,
    include_diffs: bool = False,
    diff_byte_cap: int = _DEFAULT_DIFF_CAP,
    include_bots: bool = False,
    git_binary: str = "git",
):
    """Create a dlt source that yields local Git commit history as documents.

    Args:
        repo_path: Path to a local Git clone (working tree or the repo root;
            a bare repo also works).
        refs: Git revision range(s)/refs to walk, e.g. ``["main"]`` or
            ``["HEAD"]``. Defaults to ``["HEAD"]`` (the current checked-out
            branch), matching "the history I'd actually read".
        paths: Restrict history to commits touching these paths
            (``git log -- <paths>``). Omit to include the whole repo.
        include_diffs: Include per-commit patch text (capped at
            ``diff_byte_cap`` bytes). Off by default — most of the value is
            in commit messages, and diffs are large.
        diff_byte_cap: Maximum combined diff bytes kept per commit when
            ``include_diffs=True``; the remainder is truncated with a note.
        include_bots: Include commits authored by common bot accounts
            (dependabot, renovate, github-actions). Off by default — they are
            mechanical and add little to a "why did we do this" memory.
        git_binary: Path to the ``git`` executable, mainly for test injection.

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    repo = Path(repo_path).expanduser().resolve()
    if not repo.exists():
        raise GitHistoryError(f"Path does not exist: {repo}")
    _require_git_repo(repo, git_binary)
    if _is_shallow(repo, git_binary):
        raise GitHistoryError(
            f"{repo} is a shallow clone (truncated history). Run "
            "`git fetch --unshallow` first — a shallow repo's history boundary "
            "would otherwise look like every earlier commit was deleted."
        )

    resolved_refs = list(refs) if refs else ["HEAD"]

    @dlt.resource(
        name=GITHISTORY_TABLE_NAME,
        primary_key="sha",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def git_commits():
        # Full reachable-SHA listing must succeed before anything is yielded as
        # deleted — see module docstring "Safety rule".
        reachable = _reachable_shas(repo, resolved_refs, paths, git_binary)

        dlt_state = dlt.current.resource_state()
        seen: dict = dlt_state.setdefault("seen_shas", {})

        new_shas = [sha for sha in reachable if sha not in seen]
        logger.info(
            "githistory: %d commit(s) reachable, %d new.", len(reachable), len(new_shas)
        )

        for sha in new_shas:
            commit = _show_commit(repo, sha, paths, include_diffs, diff_byte_cap, git_binary)
            if commit is None:
                continue
            if not include_bots and _looks_like_bot(commit.author_name, commit.author_email):
                seen[sha] = True
                continue
            seen[sha] = True
            yield _commit_to_row(commit, include_diffs)

        # Tombstone anything previously ingested that fell out of the
        # reachable set (rewritten/rebased/force-pushed/deleted branch).
        reachable_set = set(reachable)
        vanished = [sha for sha in list(seen) if sha not in reachable_set]
        for sha in vanished:
            del seen[sha]
            yield {"sha": sha, "_deleted": True}

        if vanished:
            logger.info("githistory: %d commit(s) no longer reachable, tombstoned.", len(vanished))

    @dlt.source(name=GITHISTORY_SOURCE_NAME)
    def _githistory():
        return git_commits

    source = _githistory()
    setattr(source, DOCUMENT_SOURCE_ATTR, GITHISTORY_SOURCE_NAME)
    return source


# ---------------------------------------------------------------------------
# git plumbing (module-private)
# ---------------------------------------------------------------------------


def _run_git(repo: Path, args: list[str], git_binary: str) -> str:
    """Run a git command in ``repo`` and return stdout as text.

    Raises GitHistoryError with the captured stderr on a non-zero exit, so a
    corrupt repo or a bad ref aborts the run loudly instead of yielding a
    partial, misleading result.
    """
    proc = subprocess.run(
        [git_binary, *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise GitHistoryError(
            f"`git {' '.join(args)}` failed in {repo} (exit {proc.returncode}): "
            f"{proc.stderr.strip()}"
        )
    return proc.stdout


def _require_git_repo(repo: Path, git_binary: str) -> None:
    try:
        _run_git(repo, ["rev-parse", "--git-dir"], git_binary)
    except GitHistoryError as exc:
        raise GitHistoryError(f"{repo} is not a Git repository: {exc}") from exc


def _is_shallow(repo: Path, git_binary: str) -> bool:
    out = _run_git(repo, ["rev-parse", "--is-shallow-repository"], git_binary)
    return out.strip() == "true"


def _reachable_shas(
    repo: Path, refs: list[str], paths: list[str] | None, git_binary: str
) -> list[str]:
    """Every commit SHA currently reachable from ``refs`` (optionally path-scoped).

    Cheap by design: no ``--format`` bodies, just SHAs, so this can run every
    sync even on a large history.
    """
    args = ["rev-list", *refs]
    if paths:
        args += ["--", *paths]
    out = _run_git(repo, args, git_binary)
    return [line for line in out.splitlines() if line]


def _show_commit(
    repo: Path,
    sha: str,
    paths: list[str] | None,
    include_diffs: bool,
    diff_byte_cap: int,
    git_binary: str,
) -> _Commit | None:
    fmt = _FIELD_SEP.join(["%H", "%an", "%ae", "%aI", "%s", "%b", "%P", "%D"])
    args = ["show", "--no-patch", f"--format={fmt}{_RECORD_SEP}", sha]
    out = _run_git(repo, args, git_binary)
    record = out.split(_RECORD_SEP, 1)[0]
    fields = record.split(_FIELD_SEP)
    if len(fields) < 8:
        logger.warning("githistory: could not parse commit %s, skipping.", sha)
        return None
    full_sha, author_name, author_email, author_date, subject, body, parents_raw, refs_raw = (
        fields[0],
        fields[1],
        fields[2],
        fields[3],
        fields[4],
        fields[5],
        fields[6],
        fields[7],
    )
    parents = parents_raw.split() if parents_raw.strip() else []
    refs = [r.strip() for r in refs_raw.split(",") if r.strip()] if refs_raw else []

    commit = _Commit(
        sha=full_sha,
        author_name=author_name,
        author_email=author_email,
        author_date=author_date,
        subject=subject,
        body=body,
        parents=parents,
        refs=refs,
    )

    stat_args = ["diff-tree", "--no-commit-id", "--numstat", "-r"]
    # Root commits (no parents) need an explicit empty-tree diff.
    if parents:
        stat_args += [sha]
    else:
        stat_args += ["--root", sha]
    if paths:
        stat_args += ["--", *paths]
    stat_out = _run_git(repo, stat_args, git_binary)
    for line in stat_out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, removed, path = parts
        try:
            commit.files.append((path, int(added), int(removed)))
        except ValueError:
            # Binary files report "-" for added/removed.
            commit.files.append((path, -1, -1))

    if include_diffs:
        commit._diff_text = _diff_text(repo, sha, parents, paths, diff_byte_cap, git_binary)  # type: ignore[attr-defined]

    return commit


def _diff_text(
    repo: Path,
    sha: str,
    parents: list[str],
    paths: list[str] | None,
    byte_cap: int,
    git_binary: str,
) -> str:
    args = ["diff-tree", "-p", "-r"]
    args += [sha] if parents else ["--root", sha]
    if paths:
        args += ["--", *paths]
    out = _run_git(repo, args, git_binary)
    encoded = out.encode("utf-8", errors="replace")
    if len(encoded) <= byte_cap:
        return out
    truncated = encoded[:byte_cap].decode("utf-8", errors="ignore")
    return truncated + f"\n...[diff truncated at {byte_cap} bytes]...\n"


def _looks_like_bot(name: str, email: str) -> bool:
    haystack = f"{name} {email}".lower()
    return any(hint in haystack for hint in _BOT_AUTHOR_HINTS)


# ---------------------------------------------------------------------------
# Row rendering (module-private, tested directly)
# ---------------------------------------------------------------------------


def _commit_to_row(commit: _Commit, include_diffs: bool) -> dict:
    is_merge = len(commit.parents) > 1
    lines = [f"# {commit.subject}", ""]
    if commit.body.strip():
        lines += [commit.body.strip(), ""]
    lines.append(f"- SHA: {commit.sha}")
    lines.append(f"- Author: {commit.author_name} <{commit.author_email}>")
    lines.append(f"- Date: {commit.author_date}")
    if is_merge:
        lines.append(f"- Merge commit, parents: {', '.join(commit.parents)}")
    if commit.refs:
        lines.append(f"- Refs: {', '.join(commit.refs)}")

    trailers = _extract_trailers(commit.body)
    if trailers:
        lines.append(f"- Trailers: {'; '.join(f'{k}: {v}' for k, v in trailers)}")

    if commit.files:
        lines.append("")
        lines.append("## Files changed")
        for path, added, removed in commit.files:
            delta = "binary" if added == -1 else f"+{added}/-{removed}"
            lines.append(f"- {path} ({delta})")

    diff_text = getattr(commit, "_diff_text", None)
    if include_diffs and diff_text:
        lines.append("")
        lines.append("## Diff")
        lines.append("```diff")
        lines.append(diff_text)
        lines.append("```")

    text = "\n".join(lines)

    return {
        "sha": commit.sha,
        "subject": commit.subject,
        "author_name": commit.author_name,
        "author_email": commit.author_email,
        "author_date": commit.author_date,
        "is_merge": is_merge,
        "parents": commit.parents,
        "refs": commit.refs,
        "trailers": dict(trailers),
        "files_changed": len(commit.files),
        "text": text,
    }


def _extract_trailers(body: str) -> list[tuple[str, str]]:
    """Parse Git trailers (``Key: value`` lines) from the end of a commit body.

    Follows the ``git interpret-trailers`` convention (``Fixes: #123``, ``Co-authored-by: ...``,
    ``Reviewed-by: ...``) of one trailer block at the end of the message,
    separated from the body by a blank line.
    """
    if not body.strip():
        return []
    blocks = [b for b in body.split("\n\n") if b.strip()]
    if not blocks:
        return []
    candidate = blocks[-1].strip().splitlines()
    trailers: list[tuple[str, str]] = []
    for line in candidate:
        if ":" not in line:
            return []
        key, _, value = line.partition(":")
        key = key.strip()
        if not key or " " in key or not key.replace("-", "").isalnum():
            return []
        trailers.append((key, value.strip()))
    return trailers
