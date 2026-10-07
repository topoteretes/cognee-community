"""GitHub dlt source.

Mirrors the Notion connector shape:
* a single dlt source named "github"
* a single dlt.resource "github_documents" with primary_key="id", write_disposition="replace"
* rows of exactly {"id","url","title","content"} documents
* DOCUMENT_SOURCE_ATTR set to "github" so cognee ingests via its document/cognify pipeline
* STABLE ids ("issue:owner/repo#42", "commit:owner/repo#sha") so unchanged rows are
  not re-cognified (incremental behaviour) and edits update, deletes drop out of snapshot
* pagination via GitHub Link headers, retry on 429/5xx honoring Retry-After / X-RateLimit-Reset
"""
from __future__ import annotations

import os
import time
from typing import Any

import httpx
import dlt

try:  # cognee exposes the source attribute name on this module
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except Exception:  # pragma: no cover - fallback used by tests without cognee installed
    DOCUMENT_SOURCE_ATTR = "dlt_source"


API_ROOT = "https://api.github.com"
USER_AGENT = "cognee-github-connector"


# --------------------------------------------------------------------------- #
# HTTP transport helpers
# --------------------------------------------------------------------------- #
def _wait_for(resp: httpx.Response) -> None:
    """Sleep honoring Retry-After / X-RateLimit-Reset for a 429/5xx response."""
    wait = 1.0
    ra = resp.headers.get("Retry-After")
    if ra and ra.isdigit():
        wait = float(ra)
    else:
        reset = resp.headers.get("X-RateLimit-Reset")
        if reset and reset.isdigit():
            wait = max(0.5, float(reset) - time.time())
    time.sleep(wait)


def _request(client: httpx.Client, method: str, url: str, params: dict | None = None) -> Any:
    """GET with retry-on-429/5xx honoring Retry-After / X-RateLimit-Reset, then raise."""
    last_exc: Exception | None = None
    for _ in range(4):  # 1 initial + 3 retries
        try:
            resp = client.request(method, url, params=params, timeout=30.0)
        except httpx.HTTPError as exc:
            last_exc = exc
            time.sleep(1)
            continue

        if resp.status_code in (429, 500, 502, 503, 504):
            _wait_for(resp)
            last_exc = httpx.HTTPStatusError(resp.text, response=resp, request=resp.request)
            continue

        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")
        if "application/json" in ctype:
            return resp.json()
        return resp.text or ""

    if last_exc:
        raise last_exc
    raise RuntimeError("request failed")  # pragma: no cover - defensive


def _link_next(resp: httpx.Response) -> str | None:
    """Parse the Link header and return the URL for rel='next', or None."""
    link_hdr = resp.headers.get("link", "")
    for part in link_hdr.split(","):
        if 'rel="next"' in part:
            return part.split(";")[0].strip().strip("<>")
    return None


def _paginate(client: httpx.Client, url: str, params: dict | None = None) -> list[Any]:
    """Follow GitHub Link headers until there is no 'next' rel; concat JSON list pages."""
    out: list[Any] = []
    while url:
        resp = client.request("GET", url, params=params, timeout=30.0)
        if resp.status_code in (429, 500, 502, 503, 504):
            _wait_for(resp)
            resp = client.request("GET", url, params=params, timeout=30.0)
        resp.raise_for_status()
        data = resp.json() if "application/json" in resp.headers.get("content-type", "") else []
        if isinstance(data, list):
            out.extend(data)
        elif isinstance(data, dict) and "items" in data:
            out.extend(data["items"])
        params = None  # only the first request carries params
        url = _link_next(resp)
    return out


# --------------------------------------------------------------------------- #
# Row builders — every row is exactly {id, url, title, content}
# --------------------------------------------------------------------------- #
def _repo_to_row(repo: dict[str, Any]) -> dict[str, Any]:
    full = repo.get("full_name") or repo.get("name") or "unknown"
    return {
        "id": f"repo:{full}",
        "url": repo.get("html_url", repo.get("url", "")),
        "title": repo.get("full_name") or repo.get("name") or "",
        "content": repo.get("description") or repo.get("readme") or "",
    }


def _issue_to_row(issue: dict[str, Any], owner_repo: str) -> dict[str, Any]:
    num = str(issue.get("number"))
    return {
        "id": f"issue:{owner_repo}#{num}",
        "url": issue.get("html_url", ""),
        "title": issue.get("title", ""),
        "content": issue.get("body") or "",
    }


def _pr_to_row(pr: dict[str, Any], owner_repo: str) -> dict[str, Any]:
    num = str(pr.get("number"))
    content = (pr.get("body") or "") + f"\n\nreview_comments: {pr.get('review_comment_count', 0)}"
    return {
        "id": f"pr:{owner_repo}#{num}",
        "url": pr.get("html_url", ""),
        "title": pr.get("title", ""),
        "content": content,
    }


def _commit_to_row(commit: dict[str, Any], owner_repo: str) -> dict[str, Any]:
    sha = commit.get("sha", "")
    msg = commit.get("commit", {}).get("message", "")
    files = commit.get("files", [])
    file_list = [f.get("filename", "") for f in files] if isinstance(files, list) else []
    content = f"{msg}\n\nchanged files:\n" + "\n".join(file_list)
    return {
        "id": f"commit:{owner_repo}#{sha}",
        "url": commit.get("html_url", commit.get("url", "")),
        "title": msg.split("\n")[0] if msg else sha,
        "content": content,
    }


def _release_to_row(release: dict[str, Any], owner_repo: str) -> dict[str, Any]:
    tag = release.get("tag_name", release.get("id", ""))
    content = f"# {release.get('name', '')}\n\n{release.get('body') or ''}"
    return {
        "id": f"release:{owner_repo}#{tag}",
        "url": release.get("html_url", release.get("url", "")),
        "title": release.get("name", str(tag)),
        "content": content,
    }


# --------------------------------------------------------------------------- #
# Comment enrichment: issue/PR body + its comments
# --------------------------------------------------------------------------- #
def _with_comments(client: httpx.Client, issue_or_pr: dict[str, Any], owner_repo: str) -> dict[str, Any]:
    is_pr = bool(issue_or_pr.get("pull_request"))
    num = str(issue_or_pr.get("number"))
    if is_pr:
        url = f"{API_ROOT}/repos/{owner_repo}/pulls/{num}/comments"
    else:
        url = f"{API_ROOT}/repos/{owner_repo}/issues/{num}/comments"
    rows = _paginate(client, url)

    comments_md = "\n\n".join(c.get("body", "") for c in rows if isinstance(c, dict) and c.get("body"))
    base_content = issue_or_pr.get("body") or ""
    if comments_md:
        if base_content:
            base_content = f"{base_content}\n\n--- comments ---\n{comments_md}"
        else:
            base_content = f"--- comments ---\n{comments_md}"

    row = _pr_to_row(issue_or_pr, owner_repo) if is_pr else _issue_to_row(issue_or_pr, owner_repo)
    row["content"] = base_content
    return row


# --------------------------------------------------------------------------- #
# Per-repo fetcher
# --------------------------------------------------------------------------- #
def _repo_rows(client: httpx.Client, owner_repo: str) -> list[dict[str, Any]]:
    repo = _request(client, "GET", f"{API_ROOT}/repos/{owner_repo}")
    if not isinstance(repo, dict):
        return []

    rows: list[dict[str, Any]] = [_repo_to_row(repo)]

    # issues (GitHub returns issues & PRs together — we keep issues, skip PRs)
    issues = _paginate(client, f"{API_ROOT}/repos/{owner_repo}/issues", {"state": "all", "per_page": 100})
    for ish in issues:
        if not isinstance(ish, dict) or ish.get("pull_request"):
            continue
        rows.append(_with_comments(client, ish, owner_repo))

    # pull requests
    prs = _paginate(client, f"{API_ROOT}/repos/{owner_repo}/pulls", {"state": "all", "per_page": 100})
    for pr in prs:
        if isinstance(pr, dict):
            rows.append(_with_comments(client, pr, owner_repo))

    # commits + the list of changed filenames (metadata only, never full diffs for large PRs)
    commits = _paginate(client, f"{API_ROOT}/repos/{owner_repo}/commits", {"per_page": 100})
    for c in commits:
        if not isinstance(c, dict) or "sha" not in c:
            continue
        files = _paginate(client, f"{API_ROOT}/repos/{owner_repo}/commits/{c['sha']}")
        file_rows = [f.get("filename", "") for f in files if isinstance(f, dict)]
        c["files"] = file_rows
        rows.append(_commit_to_row(c, owner_repo))

    # releases
    releases = _paginate(client, f"{API_ROOT}/repos/{owner_repo}/releases", {"per_page": 100})
    for r in releases:
        if isinstance(r, dict):
            rows.append(_release_to_row(r, owner_repo))

    return rows


# --------------------------------------------------------------------------- #
# Public source
# --------------------------------------------------------------------------- #
def github_source(
    token: str | None = None,
    repos: list[str] | None = None,
    orgs: list[str] | None = None,
    client: Any = None,
):
    token = token or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise ValueError("a GitHub token must be provided via arg or GITHUB_TOKEN env var")

    if client is None:
        client = httpx.Client(
            base_url=API_ROOT,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": USER_AGENT,
            },
        )

    # Resolve which owner/repo targets to ingest
    targets: list[str] = []
    if not repos and not orgs:
        # authenticated user's repos
        user = _request(client, "GET", f"{API_ROOT}/user")
        login = user.get("login") if isinstance(user, dict) else None
        if login:
            user_repos = _paginate(client, f"{API_ROOT}/user/repos", {"per_page": 100})
            targets = [r.get("full_name", "") for r in user_repos if isinstance(r, dict) and r.get("full_name")]
    else:
        if repos:
            targets.extend(repos)
        for org in orgs or []:
            org_repos = _paginate(client, f"{API_ROOT}/orgs/{org}/repos", {"per_page": 100})
            for r in org_repos:
                if isinstance(r, dict) and r.get("full_name"):
                    targets.append(r["full_name"])

    @dlt.resource(name="github_documents", primary_key="id", write_disposition="replace")
    def github_documents():
        seen: set[str] = set()
        for owner_repo in targets:
            try:
                for row in _repo_rows(client, owner_repo):
                    rid = row["id"]
                    if rid in seen:
                        continue
                    seen.add(rid)
                    yield row
            except Exception as exc:  # pragma: no cover - log and continue per repo
                print(f"[github] skipping {owner_repo}: {exc}")


    @dlt.source(name="github")
    def _github():
        return github_documents

    source = _github()
    setattr(source, DOCUMENT_SOURCE_ATTR, "github")
    return source
