"""OpenSiro canonical Index snapshots — source-only, no Cognee SDK or network.

Prototype research package. Strict fail-closed admission and literal VSM grades.
Data obtained from a PINNED Git tree, not mutable checkout HEAD.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

SHA40 = re.compile(r"^[0-9a-f]{40}$")
QUESTION_SCALAR = re.compile(r"^(autonomy_[a-z0-9_]+:[ \t]*)\?[ \t]*$", re.MULTILINE)
FUNCTIONS = ("S1", "S2", "S3", "S3_STAR", "S4", "S5")
FIELD = {f: "autonomy_" + f.lower() for f in FUNCTIONS}
ALLOWED = frozenset(("A", "B", "C", "P", "A(P)", "B(P)", "C(P)", "—", "?"))
STATUSES = frozenset(("included", "excluded-no-agentic-vsm", "proposed"))
HEADING = re.compile(r"(?m)^## Primary evidence[ \t]*$")
NEXT_SECTION = re.compile(r"(?m)^##[ \t]+")
BULLET = re.compile(r"(?m)^[ \t]*[-*][ \t]+(.+?)\s*$")


class SourceError(ValueError):
    """Snapshot cannot safely be published or projected."""


class UniqueLoader(yaml.SafeLoader):
    pass


def _no_duplicates(loader, node):
    result = {}
    for k, v in node.value:
        key = loader.construct_object(k, deep=True)
        if key in result:
            raise SourceError(f"duplicate YAML field: {key!r}")
        result[key] = loader.construct_object(v, deep=True)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates)


@dataclass(frozen=True)
class Record:
    harness_id: str
    status: str
    project_name: str
    repository: str
    review_ref: str
    profile_version: str
    assessment_procedure_version: str
    states: tuple[tuple[str, str], ...]
    raw_bytes: bytes
    sha256: str
    assessment_url: str
    primary_evidence: tuple[str, ...]

    @property
    def external_id(self) -> str:
        return "opensiro/vsm-harness-index:" + self.harness_id

    @property
    def version_id(self) -> str:
        return self.external_id + ":sha256:" + self.sha256

    def grades(self) -> dict[str, str]:
        return dict(self.states)


@dataclass(frozen=True)
class Snapshot:
    commit: str
    accepted: Mapping[str, Record]
    excluded: Mapping[str, Record]
    proposed: Mapping[str, Record]
    catalog_ids: frozenset[str]
    unquoted_question_count: int

    def stats(self):
        return {
            "commit": self.commit,
            "accepted": len(self.accepted),
            "excluded": len(self.excluded),
            "proposed": len(self.proposed),
            "catalog": len(self.catalog_ids),
            "unquoted_question_files": self.unquoted_question_count,
            "literal_grades": {
                x: sum(v == x for rec in self.accepted.values() for _, v in rec.states)
                for x in sorted(ALLOWED)
                if any(x == v for rec in self.accepted.values() for _, v in rec.states)
            },
        }


def _require_str(obj: dict, key: str, ctx: str) -> str:
    val = obj.get(key)
    if not isinstance(val, str) or not val.strip():
        raise SourceError(f"{ctx}: missing/nonstring {key}")
    return val


def _explicit_primary_evidence(text: str) -> tuple[str, ...]:
    match = HEADING.search(text)
    if match is None:
        return ()
    start = match.end()
    rest = text[start:]
    following = NEXT_SECTION.search(rest)
    body = rest[: following.start()] if following else rest
    return tuple(x.strip() for x in BULLET.findall(body))


def assessment_paths(harness_id: str) -> tuple[str, str]:
    """Both canonical historical layouts; reject every other path shape."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", harness_id):
        raise SourceError("Invalid assessment harness_id")
    return (
        f"assessments/{harness_id}.md",
        f"assessments/{harness_id[:2]}/{harness_id}.md",
    )


def parse_assessment(
    path: str, content: bytes, commit: str, *, require_contract: bool = True
) -> tuple[Record, bool]:
    """Never infer function->evidence links; raw bytes are authoritative."""
    if not SHA40.fullmatch(commit):
        raise SourceError("commit must be exact 40-char SHA")
    try:
        txt = content.decode("utf-8")
    except UnicodeDecodeError as ex:
        raise SourceError(f"{path}: invalid UTF-8") from ex
    if not txt.startswith("---\n") or "\n---\n" not in txt[4:]:
        raise SourceError(f"{path}: frontmatter boundary missing")
    front = txt.split("---\n", 2)[1]
    bad_question = bool(QUESTION_SCALAR.search(front))
    compatible = QUESTION_SCALAR.sub(r'\1"?"', front)
    # YAML can parse an all-numeric 40-character Git SHA as a number,
    # dropping leading zeros. Preserve only this exact valid SHA scalar.
    compatible = re.sub(
        r"^(review_ref:[ \t]*)([0-9a-f]{40})[ \t]*$", r'\1"\2"', compatible, flags=re.MULTILINE
    )
    try:
        meta = yaml.load(compatible, Loader=UniqueLoader)
    except (yaml.YAMLError, SourceError) as ex:
        raise SourceError(f"{path}: invalid frontmatter: {ex}") from ex
    if not isinstance(meta, dict):
        raise SourceError(f"{path}: frontmatter must be a map")
    hid = _require_str(meta, "harness_id", path)
    if path not in assessment_paths(hid):
        raise SourceError(f"{path}: invalid/mismatched harness_id: {hid!r}")
    status = _require_str(meta, "status", path)
    if status not in STATUSES:
        raise SourceError(f"{path}: unrecognized status {status!r}")
    if status == "included" and require_contract:
        for k in ("profile_version", "assessment_procedure_version"):
            _require_str(meta, k, path)
    states = []
    for display in FUNCTIONS:
        field = FIELD[display]
        val = meta.get(field)
        if type(val) is not str or val not in ALLOWED:
            raise SourceError(f"{path}: illegal {field} value: {val!r}")
        states.append((display, val))
    review = _require_str(meta, "review_ref", path)
    if not SHA40.fullmatch(review):
        raise SourceError(f"{path}: review_ref must be a full upstream Git SHA")
    record = Record(
        harness_id=hid,
        status=status,
        project_name=_require_str(meta, "project_name", path),
        repository=_require_str(meta, "repository", path),
        review_ref=review,
        profile_version=str(meta.get("profile_version") or ""),
        assessment_procedure_version=str(meta.get("assessment_procedure_version") or ""),
        states=tuple(states),
        raw_bytes=content,
        sha256=hashlib.sha256(content).hexdigest(),
        assessment_url=f"https://github.com/opensiro/vsm-harness-index/blob/{commit}/{path}",
        primary_evidence=_explicit_primary_evidence(txt),
    )
    return record, bad_question


def _git(repo: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, timeout=90
        )
        return result.stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as ex:
        raise SourceError(f"Cannot read immutable Git tree, command={args[:2]!r}") from ex


def read_snapshot(repo: Path | str, commit: str) -> Snapshot:
    """Fail closed if any file/canonical catalog member disagrees with admission."""
    repo = Path(repo).resolve()
    if not SHA40.fullmatch(commit):
        raise SourceError("Only a pinned, full 40-character SHA is permitted")
    resolved = _git(repo, "rev-parse", "--verify", commit + "^{commit}").decode().strip()
    if resolved != commit:
        raise SourceError("Pinned commit resolves to a different revision")
    rawpaths = _git(repo, "ls-tree", "-r", "--name-only", commit, "assessments")
    paths = [
        p
        for p in rawpaths.decode().splitlines()
        if p.startswith("assessments/") and p.endswith(".md")
    ]
    if not paths:
        raise SourceError("No assessments in Git tree")
    rawcat = _git(repo, "show", commit + ":data/catalog.psv").decode("utf-8")
    reader = csv.DictReader(io.StringIO(rawcat), delimiter="|")
    if not reader.fieldnames or "harness_id" not in reader.fieldnames:
        raise SourceError("missing canonical catalog harness_id")
    cat = list(reader)
    catalog_ids = [x.get("harness_id", "") for x in cat]
    if len(catalog_ids) != len(set(catalog_ids)) or not all(catalog_ids):
        raise SourceError("duplicate/missing canonical catalog ID")
    lookup = {}
    groups = {"included": {}, "excluded-no-agentic-vsm": {}, "proposed": {}}
    question_files = 0
    for path in sorted(paths):
        rec, question = parse_assessment(path, _git(repo, "show", commit + ":" + path), commit)
        if rec.harness_id in lookup:
            raise SourceError(f"duplicate assessment ID {rec.harness_id}")
        lookup[rec.harness_id] = rec
        groups[rec.status][rec.harness_id] = rec
        question_files += question
    admitted_ids = set(groups["included"]) | set(groups["excluded-no-agentic-vsm"])
    if admitted_ids != set(catalog_ids):
        missing = sorted(set(catalog_ids) - admitted_ids)
        extraneous = sorted(admitted_ids - set(catalog_ids))
        raise SourceError(
            "Admitted/canonical catalog mismatch; "
            f"missing_records={missing[:5]} extraneous_records={extraneous[:5]}"
        )
    if set(groups["proposed"]) & set(catalog_ids):
        raise SourceError("Proposed records leaked into canonical catalog")
    return Snapshot(
        commit=commit,
        accepted=groups["included"],
        excluded=groups["excluded-no-agentic-vsm"],
        proposed=groups["proposed"],
        catalog_ids=frozenset(catalog_ids),
        unquoted_question_count=question_files,
    )


def diff_snapshots(old: Snapshot, new: Snapshot) -> dict:
    """Immutable source plan, no writes, returns admitted-only effects."""
    a, b = old.accepted, new.accepted
    a_ids, b_ids = set(a), set(b)
    common = a_ids & b_ids
    changed = sorted(x for x in common if a[x].sha256 != b[x].sha256)
    unchanged = sorted(x for x in common if a[x].sha256 == b[x].sha256)
    added = sorted(b_ids - a_ids)
    retired = sorted(a_ids - b_ids)
    grades_changed = {
        x: {
            f: {"old": a[x].grades()[f], "new": b[x].grades()[f]}
            for f in FUNCTIONS
            if a[x].grades()[f] != b[x].grades()[f]
        }
        for x in changed
    }
    return {
        "old_commit": old.commit,
        "new_commit": new.commit,
        "old_included": len(a),
        "new_included": len(b),
        "added": added,
        "retired": retired,
        "changed": changed,
        "unchanged_count": len(unchanged),
        "changed_grades": {x: v for x, v in grades_changed.items() if v},
        "changed_same_review_ref": sorted(x for x in changed if a[x].review_ref == b[x].review_ref),
        "proposed_to_included": sorted(set(old.proposed) & b_ids),
        "included_to_proposed": sorted(a_ids & set(new.proposed)),
        "included_to_excluded": sorted(a_ids & set(new.excluded)),
    }


def inspect_one_historical_record(repo: Path | str, commit: str, harness_id: str) -> Record:
    """Diagnostic per-file history, NOT a valid/complete canonical snapshot.

    Older Index revisions can legitimately lack today's admission contract.
    The full-snapshot validator must keep rejecting those revisions.
    """
    if not SHA40.fullmatch(commit) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", harness_id):
        raise SourceError("pin exact SHA and safe harness ID")
    repo = Path(repo).resolve()
    resolved = _git(repo, "rev-parse", "--verify", commit + "^{commit}").decode().strip()
    if resolved != commit:
        raise SourceError("historical ref does not resolve exactly")
    present = set(
        _git(repo, "ls-tree", "-r", "--name-only", commit, "assessments").decode().splitlines()
    )
    matches = [path for path in assessment_paths(harness_id) if path in present]
    if len(matches) != 1:
        raise SourceError("Historical assessment must have exactly one canonical path")
    path = matches[0]
    record, _ = parse_assessment(
        path, _git(repo, "show", commit + ":" + path), commit, require_contract=False
    )
    return record
