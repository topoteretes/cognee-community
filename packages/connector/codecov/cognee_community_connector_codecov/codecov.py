"""Codecov coverage-history data-source connector for cognee.

Extracts repository test coverage metrics, commit-level trends, and file-level
coverage gaps from Codecov (api.codecov.io) or offline snapshots into clean
markdown documents for Cognee graph indexing and agent RAG queries.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Generator, Iterable, List, Optional, Union

DOCUMENT_SOURCE_ATTR = "codecov"


def _format_coverage_badge(percent: Optional[float]) -> str:
    """Render a visual markdown status badge based on test coverage percentage."""
    if percent is None:
        return "⏳ **PENDING / NO REPORT**"
    if percent >= 90.0:
        return f"🟢 **EXCELLENT ({percent:.2f}%)**"
    if percent >= 80.0:
        return f"🟡 **ACCEPTABLE ({percent:.2f}%)**"
    return f"🔴 **AT RISK / DEFICIENT ({percent:.2f}%)**"


def format_repository_coverage_document(
    repo_data: Dict[str, Any],
    service: str = "github",
    owner: str = "",
    repo: str = "",
) -> str:
    """Render a repository-wide coverage summary into a clean markdown document."""
    full_name = f"{owner}/{repo}" if owner and repo else repo_data.get("name", "Unknown Repo")
    totals = repo_data.get("totals", {})
    coverage = totals.get("coverage")
    lines = totals.get("lines", 0)
    hits = totals.get("hits", 0)
    misses = totals.get("misses", 0)
    partials = totals.get("partials", 0)
    default_branch = repo_data.get("default_branch", "main")
    active = repo_data.get("active", True)

    badge = _format_coverage_badge(coverage)

    doc_lines = [
        f"# [Codecov Repository Summary] {full_name}",
        "",
        "## Overall Test Coverage",
        f"- **Repository:** `{service}/{full_name}`",
        f"- **Coverage Rating:** {badge}",
        f"- **Default Branch:** `{default_branch}`",
        f"- **Total Executable Lines:** {lines:,}",
        f"- **Covered Lines (Hits):** {hits:,}",
        f"- **Uncovered Lines (Misses):** {misses:,}",
        f"- **Partial / Branch Coverage:** {partials:,}",
        f"- **Repository Status:** `{'Active' if active else 'Archived'}`",
    ]

    branches = repo_data.get("monitored_branches", [])
    if branches:
        doc_lines.extend([
            "",
            "## Monitored Branches",
            ", ".join(f"`{b}`" for b in branches),
        ])

    return "\n".join(doc_lines)


def format_commit_coverage_document(
    commit: Dict[str, Any],
    service: str = "github",
    owner: str = "",
    repo: str = "",
) -> str:
    """Render a commit-level coverage snapshot and file-level gap analysis."""
    sha = commit.get("commitid") or commit.get("sha") or "unknown_sha"
    short_sha = sha[:8]
    message = (commit.get("message") or "No commit message provided").split("\n")[0].strip()
    author = commit.get("author", {}).get("username") or commit.get("author_name") or "Automated Contributor"
    timestamp = commit.get("timestamp") or commit.get("created_on") or "N/A"
    branch = commit.get("branch") or "main"

    state = commit.get("state", "complete").upper()
    totals = commit.get("totals", {})
    coverage = totals.get("coverage")
    lines = totals.get("lines", 0)
    hits = totals.get("hits", 0)
    misses = totals.get("misses", 0)

    badge = _format_coverage_badge(coverage)

    doc_lines = [
        f"# [Codecov Commit Report] `{short_sha}` — {message}",
        "",
        "## Commit Overview",
        f"- **Commit SHA:** `{sha}`",
        f"- **Repository:** `{service}/{owner}/{repo}`",
        f"- **Branch:** `{branch}`",
        f"- **Author:** `{author}`",
        f"- **Committed At:** `{timestamp}`",
        f"- **Report Status:** `{state}`",
        f"- **Coverage Rating:** {badge}",
        f"- **Lines / Hits / Misses:** {lines:,} total | {hits:,} hits | {misses:,} misses",
    ]

    file_gaps = commit.get("file_gaps", [])
    if file_gaps:
        doc_lines.extend([
            "",
            "## Critical File-Level Coverage Gaps",
            "| File Path | Coverage | Missed Lines | Complexity / Notes |",
            "|---|---|---|---|",
        ])
        for gap in file_gaps:
            fpath = gap.get("path", "file.py")
            f_cov = gap.get("coverage", 0.0)
            f_miss = gap.get("misses", 0)
            notes = gap.get("notes", "Missing test assertions")
            doc_lines.append(f"| `{fpath}` | {f_cov:.1f}% | {f_miss} | {notes} |")

    return "\n".join(doc_lines)


def parse_codecov_reports(
    data: Dict[str, Any],
    service: str = "github",
    owner: str = "",
    repo: str = "",
) -> List[Dict[str, Any]]:
    """Parse repository totals and commit coverage history into indexed documents."""
    documents = []
    repo_scope = f"{service}:{owner}:{repo}" if owner and repo else "codecov_repo"

    # 1. Repository-wide summary document
    repo_data = data.get("repository", data)
    repo_doc_text = format_repository_coverage_document(repo_data, service=service, owner=owner, repo=repo)
    repo_hash = hashlib.sha256(repo_doc_text.encode("utf-8")).hexdigest()
    documents.append({
        "id": f"codecov_{service}_{owner}_{repo}_summary",
        "type": "repo_summary",
        "entity_id": f"{owner}/{repo}",
        "text": repo_doc_text,
        "hash": repo_hash,
        "node_sets": [f"codecov:repo:{repo_scope}"],
        "metadata": {
            "source": DOCUMENT_SOURCE_ATTR,
            "service": service,
            "owner": owner,
            "repo": repo,
            "coverage": repo_data.get("totals", {}).get("coverage"),
        },
    })

    # 2. Commit coverage history documents
    commits = data.get("commits", [])
    for c in commits:
        sha = c.get("commitid") or c.get("sha") or "unknown_sha"
        branch = c.get("branch") or "main"
        doc_text = format_commit_coverage_document(c, service=service, owner=owner, repo=repo)
        content_hash = hashlib.sha256(doc_text.encode("utf-8")).hexdigest()

        node_sets = [
            f"codecov:repo:{repo_scope}",
            f"codecov:commit:{sha}",
            f"codecov:branch:{branch}",
        ]

        documents.append({
            "id": f"codecov_{service}_{owner}_{repo}_commit_{sha}",
            "type": "commit_report",
            "entity_id": sha,
            "text": doc_text,
            "hash": content_hash,
            "node_sets": node_sets,
            "metadata": {
                "source": DOCUMENT_SOURCE_ATTR,
                "service": service,
                "owner": owner,
                "repo": repo,
                "commit_sha": sha,
                "branch": branch,
                "coverage": c.get("totals", {}).get("coverage"),
            },
        })

    return documents


def codecov_source(
    service: str = "github",
    owner: str = "",
    repo: str = "",
    branch: str = "main",
    api_token: Optional[str] = None,
    history_limit: int = 10,
    snapshot_data: Optional[Union[Dict[str, Any], str, Path]] = None,
) -> Any:
    """Cognee DLT source for indexing Codecov test coverage reports into agent memory."""
    if isinstance(snapshot_data, (str, Path)):
        p = Path(snapshot_data)
        if not p.exists():
            raise FileNotFoundError(f"Codecov snapshot file not found: {p}")
        with open(p, "r", encoding="utf-8") as f:
            raw_payload = json.load(f)
    elif isinstance(snapshot_data, dict):
        raw_payload = snapshot_data
    elif owner and repo:
        import requests

        clean_base = f"https://api.codecov.io/api/v2/{service}/{owner}/repos/{repo}"
        headers = {"Accept": "application/json"}
        if api_token:
            headers["Authorization"] = f"Bearer {api_token}"

        # 1. Fetch Repository Totals
        tot_res = requests.get(f"{clean_base}/totals/", headers=headers, timeout=25)
        tot_res.raise_for_status()
        repo_totals = tot_res.json()

        # 2. Fetch Commits History
        commits_url = f"{clean_base}/commits/?branch={branch}&limit={history_limit}"
        c_res = requests.get(commits_url, headers=headers, timeout=30)
        c_res.raise_for_status()
        commits_data = c_res.json().get("results", [])

        raw_payload = {
            "repository": {
                "name": f"{owner}/{repo}",
                "totals": repo_totals,
                "default_branch": branch,
            },
            "commits": commits_data,
        }
    else:
        raise ValueError("Must provide either 'snapshot_data' or both 'owner' and 'repo'.")

    parsed_docs = parse_codecov_reports(raw_payload, service=service, owner=owner, repo=repo)

    try:
        import dlt
    except ImportError as e:
        raise ImportError(
            "The Codecov connector requires dlt: pip install 'cognee-community-connector-codecov' or pip install 'dlt[sqlalchemy]'."
        ) from e

    @dlt.source(name="codecov")
    def _source() -> Generator[Any, None, None]:
        @dlt.resource(name="coverage_reports", write_disposition="replace")
        def _reports() -> Generator[Dict[str, Any], None, None]:
            for doc in parsed_docs:
                yield {
                    "id": doc["id"],
                    "text": doc["text"],
                    "cognee_document_source": DOCUMENT_SOURCE_ATTR,
                    "cognee_node_set": doc.get("node_sets", []),
                    "metadata": doc.get("metadata", {}),
                }

        yield _reports()

    return _source()
