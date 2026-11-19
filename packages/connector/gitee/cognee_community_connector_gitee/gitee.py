"""Gitee Data-Source Connector for Cognee.

Ingests repositories, issues, pull requests, commits, and releases from Gitee REST API v5
into Cognee knowledge graph and vector memory.
Resolves issue: cognee#6364.
"""

import os
import time
import json
import hashlib
import urllib.request
import urllib.parse
import urllib.error
from typing import Dict, List, Any, Optional, Iterator, Union, Set
from datetime import datetime, timezone

# Optional dlt import for Cognee ecosystem compatibility
try:
    import dlt
    _has_dlt = True
except ImportError:
    dlt = None
    _has_dlt = False

def _optional_source(func):
    if _has_dlt and hasattr(dlt, "source"):
        return dlt.source(func)
    return func

def _optional_resource(*args, **kwargs):
    def decorator(func):
        if _has_dlt and hasattr(dlt, "resource"):
            return dlt.resource(*args, **kwargs)(func)
        return func
    return decorator


class GiteeClient:
    """Client for Gitee Open Source REST API v5."""

    def __init__(
        self,
        access_token: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: int = 30,
        user_agent: Optional[str] = None,
    ):
        self.access_token = access_token or os.getenv("GITEE_ACCESS_TOKEN") or os.getenv("GITEE_API_TOKEN")
        self.base_url = (base_url or os.getenv("GITEE_BASE_URL") or "https://gitee.com/api/v5").rstrip("/")
        self.timeout = timeout
        self.user_agent = user_agent or "GiteeCogneeConnector/1.0 (https://github.com/topoteretes/cognee-community)"

    def _get_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }
        if self.access_token:
            headers["Authorization"] = f"token {self.access_token}"
        return headers

    def _request(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Union[Dict[str, Any], List[Any]]:
        endpoint = endpoint.lstrip("/")
        query_params = dict(params or {})

        # If token is provided, pass via param fallback if header is stripped
        if self.access_token and "access_token" not in query_params:
            query_params["access_token"] = self.access_token

        query_string = urllib.parse.urlencode({k: v for k, v in query_params.items() if v is not None})
        url = f"{self.base_url}/{endpoint}"
        if query_string:
            url = f"{url}?{query_string}"

        req = urllib.request.Request(url, headers=self._get_headers(), method="GET")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = resp.read().decode("utf-8")
                return json.loads(data) if data else {}
        except urllib.error.HTTPError as e:
            err_msg = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
            if e.code == 401:
                raise PermissionError(f"Gitee API authentication failed (HTTP 401): {err_msg}")
            elif e.code == 404:
                raise FileNotFoundError(f"Gitee resource not found (HTTP 404): {url} - {err_msg}")
            elif e.code == 429:
                raise ConnectionRefusedError(f"Gitee API rate limit reached (HTTP 429): {err_msg}")
            raise RuntimeError(f"Gitee API request error (HTTP {e.code}): {err_msg}")
        except urllib.error.URLError as e:
            raise ConnectionError(f"Failed to connect to Gitee API ({url}): {e.reason}")

    def get_user_repos(self, visibility: str = "all", per_page: int = 50, page: int = 1) -> List[Dict[str, Any]]:
        """List repositories accessible to the authenticated user."""
        res = self._request("user/repos", {"visibility": visibility, "per_page": min(per_page, 50), "page": page})
        return res if isinstance(res, list) else []

    def get_repo(self, owner: str, repo: str) -> Dict[str, Any]:
        """Fetch repository metadata."""
        res = self._request(f"repos/{owner}/{repo}")
        return res if isinstance(res, dict) else {}

    def get_issues(
        self,
        owner: str,
        repo: str,
        state: str = "all",
        since: Optional[str] = None,
        per_page: int = 50,
        page: int = 1,
    ) -> List[Dict[str, Any]]:
        """List issues for a repository."""
        params = {"state": state, "per_page": min(per_page, 50), "page": page}
        if since:
            params["since"] = since
        res = self._request(f"repos/{owner}/{repo}/issues", params)
        return res if isinstance(res, list) else []

    def get_pull_requests(
        self,
        owner: str,
        repo: str,
        state: str = "all",
        per_page: int = 50,
        page: int = 1,
    ) -> List[Dict[str, Any]]:
        """List pull requests for a repository."""
        res = self._request(f"repos/{owner}/{repo}/pulls", {"state": state, "per_page": min(per_page, 50), "page": page})
        return res if isinstance(res, list) else []

    def get_commits(
        self,
        owner: str,
        repo: str,
        since: Optional[str] = None,
        per_page: int = 50,
        page: int = 1,
    ) -> List[Dict[str, Any]]:
        """List commits for a repository."""
        params = {"per_page": min(per_page, 50), "page": page}
        if since:
            params["since"] = since
        res = self._request(f"repos/{owner}/{repo}/commits", params)
        return res if isinstance(res, list) else []

    def get_releases(
        self,
        owner: str,
        repo: str,
        per_page: int = 50,
        page: int = 1,
    ) -> List[Dict[str, Any]]:
        """List releases for a repository."""
        res = self._request(f"repos/{owner}/{repo}/releases", {"per_page": min(per_page, 50), "page": page})
        return res if isinstance(res, list) else []

    def paginate(self, fetch_fn, max_pages: int = 10, **kwargs) -> List[Dict[str, Any]]:
        """Helper to walk through pages up to max_pages or until a short page returns."""
        items = []
        per_page = kwargs.get("per_page", 50)
        for page in range(1, max_pages + 1):
            kwargs["page"] = page
            batch = fetch_fn(**kwargs)
            if not batch:
                break
            items.extend(batch)
            if len(batch) < per_page:
                break
        return items


def format_gitee_document(item_type: str, item: Dict[str, Any], owner: str, repo: str) -> Dict[str, Any]:
    """Formats Gitee entities into standard Cognee document models with stable content-hash data_id."""
    raw_id = str(item.get("id") or item.get("number") or item.get("sha") or item.get("name") or "unknown")
    content_hash = hashlib.sha256(f"gitee:{owner}/{repo}:{item_type}:{raw_id}".encode("utf-8")).hexdigest()

    if item_type == "repository":
        title = f"Gitee Repository: {item.get('full_name') or f'{owner}/{repo}'}"
        body = [
            f"# {title}",
            f"- Owner: {owner}",
            f"- Name: {repo}",
            f"- Description: {item.get('description', 'No description provided.')}",
            f"- Default Branch: {item.get('default_branch', 'master')}",
            f"- Open Issues: {item.get('open_issues_count', 0)}",
            f"- Stars: {item.get('stargazers_count', 0)} | Forks: {item.get('forks_count', 0)}",
            f"- Created At: {item.get('created_at', 'N/A')}",
            f"- Updated At: {item.get('updated_at', 'N/A')}",
            f"- URL: {item.get('html_url', f'https://gitee.com/{owner}/{repo}')}",
        ]
        text_content = "\n".join(body)

    elif item_type == "issue":
        num = item.get("number", "N/A")
        title = f"Gitee Issue #{num}: {item.get('title', 'Untitled')}"
        body = [
            f"# {title}",
            f"- Repository: {owner}/{repo}",
            f"- State: {item.get('state', 'open')}",
            f"- Author: {item.get('user', {}).get('login', 'Unknown')}",
            f"- Created At: {item.get('created_at', 'N/A')}",
            f"- Updated At: {item.get('updated_at', 'N/A')}",
            f"- URL: {item.get('html_url', '')}",
            "",
            "## Description",
            item.get("body") or "No issue description provided.",
        ]
        text_content = "\n".join(body)

    elif item_type == "pull_request":
        num = item.get("number", "N/A")
        title = f"Gitee Pull Request #{num}: {item.get('title', 'Untitled')}"
        body = [
            f"# {title}",
            f"- Repository: {owner}/{repo}",
            f"- State: {item.get('state', 'open')}",
            f"- Author: {item.get('user', {}).get('login', 'Unknown')}",
            f"- Head Branch: {item.get('head', {}).get('ref', 'unknown')} -> Base: {item.get('base', {}).get('ref', 'master')}",
            f"- Mergeable: {item.get('mergeable', True)}",
            f"- Created At: {item.get('created_at', 'N/A')}",
            f"- Updated At: {item.get('updated_at', 'N/A')}",
            f"- URL: {item.get('html_url', '')}",
            "",
            "## Summary",
            item.get("body") or "No pull request summary provided.",
        ]
        text_content = "\n".join(body)

    elif item_type == "commit":
        sha = item.get("sha", "unknown")[:8]
        msg = item.get("commit", {}).get("message", "No commit message")
        title = f"Gitee Commit {sha} in {owner}/{repo}"
        author = item.get("commit", {}).get("author", {}).get("name", "Unknown")
        date = item.get("commit", {}).get("author", {}).get("date", "N/A")
        body = [
            f"# {title}",
            f"- Repository: {owner}/{repo}",
            f"- Commit SHA: {item.get('sha')}",
            f"- Author: {author}",
            f"- Date: {date}",
            f"- URL: {item.get('html_url', '')}",
            "",
            "## Message",
            msg,
        ]
        text_content = "\n".join(body)

    elif item_type == "release":
        tag = item.get("tag_name") or item.get("name") or "latest"
        title = f"Gitee Release {tag} in {owner}/{repo}"
        body = [
            f"# {title}",
            f"- Repository: {owner}/{repo}",
            f"- Tag Name: {item.get('tag_name')}",
            f"- Author: {item.get('author', {}).get('login', 'Unknown')}",
            f"- Published At: {item.get('created_at', 'N/A')}",
            "",
            "## Release Notes",
            item.get("body") or "No release notes provided.",
        ]
        text_content = "\n".join(body)

    else:
        title = f"Gitee {item_type} in {owner}/{repo}"
        text_content = json.dumps(item, indent=2)

    return {
        "data_id": content_hash,
        "name": title,
        "text": text_content,
        "metadata": {
            "source": "gitee",
            "item_type": item_type,
            "owner": owner,
            "repo": repo,
            "raw_id": raw_id,
            "url": item.get("html_url", ""),
            "updated_at": item.get("updated_at") or item.get("created_at") or datetime.now(timezone.utc).isoformat(),
        },
        "write_disposition": "replace",
    }


class GiteeConnector:
    """Orchestrates ingestion of Gitee data into Cognee document memory."""

    def __init__(self, client: Optional[GiteeClient] = None, access_token: Optional[str] = None):
        self.client = client or GiteeClient(access_token=access_token)

    def fetch_repository(
        self,
        owner: str,
        repo: str,
        include_issues: bool = True,
        include_pulls: bool = True,
        include_commits: bool = True,
        include_releases: bool = True,
        since: Optional[str] = None,
        max_pages: int = 5,
    ) -> List[Dict[str, Any]]:
        """Fetches and formats all data for a specific Gitee repository."""
        documents = []

        # 1. Fetch Repository Overview
        try:
            repo_meta = self.client.get_repo(owner, repo)
            if repo_meta:
                documents.append(format_gitee_document("repository", repo_meta, owner, repo))
        except Exception:
            pass

        # 2. Fetch Issues
        if include_issues:
            issues = self.client.paginate(self.client.get_issues, max_pages=max_pages, owner=owner, repo=repo, state="all", since=since)
            for iss in issues:
                documents.append(format_gitee_document("issue", iss, owner, repo))

        # 3. Fetch Pull Requests
        if include_pulls:
            pulls = self.client.paginate(self.client.get_pull_requests, max_pages=max_pages, owner=owner, repo=repo, state="all")
            for pr in pulls:
                documents.append(format_gitee_document("pull_request", pr, owner, repo))

        # 4. Fetch Commits
        if include_commits:
            commits = self.client.paginate(self.client.get_commits, max_pages=max_pages, owner=owner, repo=repo, since=since)
            for c in commits:
                documents.append(format_gitee_document("commit", c, owner, repo))

        # 5. Fetch Releases
        if include_releases:
            releases = self.client.paginate(self.client.get_releases, max_pages=max_pages, owner=owner, repo=repo)
            for rel in releases:
                documents.append(format_gitee_document("release", rel, owner, repo))

        return documents


def fetch_gitee_repository_data(
    owner: str,
    repo: str,
    access_token: Optional[str] = None,
    since: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Convenience helper to extract structured Cognee documents from a Gitee repository."""
    connector = GiteeConnector(access_token=access_token)
    return connector.fetch_repository(owner=owner, repo=repo, since=since)


@_optional_source
def gitee_source(
    repositories: List[str],
    access_token: Optional[str] = None,
    since: Optional[str] = None,
):
    """Cognee dlt-compatible source generator for Gitee data ingestion."""
    connector = GiteeConnector(access_token=access_token)

    @_optional_resource(name="gitee_documents", write_disposition="replace")
    def gitee_documents() -> Iterator[Dict[str, Any]]:
        for entry in repositories:
            parts = entry.strip("/").split("/")
            if len(parts) >= 2:
                owner, repo = parts[0], parts[1]
                docs = connector.fetch_repository(owner, repo, since=since)
                for doc in docs:
                    yield doc

    return gitee_documents()
