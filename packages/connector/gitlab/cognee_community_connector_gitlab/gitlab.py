"""GitLab document source scaffold; API extraction and sync are not implemented yet."""

from collections.abc import Sequence
from typing import Any, Literal

import httpx

ResourceType = Literal["issues", "merge_requests", "comments", "wiki"]


class GitLabAPIError(RuntimeError):
    """GitLab could not complete a required read; never treat this as empty data."""


def _get_json(client: httpx.Client, path: str, **kwargs) -> Any:
    """Make a GitLab GET request and parse its successful JSON response."""
    try:
        response = client.get(path, **kwargs)
    except httpx.RequestError:
        raise GitLabAPIError("GitLab request failed: network error or timeout.") from None

    if response.status_code == 401:
        raise GitLabAPIError("GitLab rejected the token: invalid, expired, or revoked.")
    if response.status_code == 403:
        raise GitLabAPIError("GitLab denied access. Check read_api scope and project permissions.")
    if response.status_code != 200:
        raise GitLabAPIError(f"GitLab read failed with HTTP {response.status_code}.")
    try:
        return response.json()
    except ValueError:
        raise GitLabAPIError("GitLab returned invalid JSON.") from None


def _validate_token(client: httpx.Client) -> dict[str, Any]:
    """Validate the authenticated legacy PAT without fetching the user profile."""
    metadata = _get_json(client, "personal_access_tokens/self")
    if not isinstance(metadata, dict):
        raise GitLabAPIError("GitLab returned invalid token metadata.")
    # GitLab's active status already accounts for expiry and revocation.
    if metadata.get("active") is not True:
        raise GitLabAPIError("GitLab token is inactive.")

    scopes = metadata.get("scopes")
    if (
        not isinstance(scopes, list)
        or not all(isinstance(scope, str) for scope in scopes)
        or not {"read_api", "api"}.intersection(scopes)
    ):
        raise GitLabAPIError("GitLab token requires the read_api scope (or broader api scope).")
    return metadata


def _discover_projects(client: httpx.Client) -> list[dict[str, Any]]:
    """List accessible projects, including public ones, using keyset pagination.

    Advance by project ID rather than following server-supplied URLs, so the
    credential stays on the configured instance. Failed pages abort discovery.
    """
    projects = []
    last_id = 0
    while True:
        params = {
            "pagination": "keyset",
            "order_by": "id",
            "sort": "asc",
            "per_page": 100,
        }
        if last_id:
            params["id_after"] = last_id
        page = _get_json(client, "projects", params=params)
        if not isinstance(page, list):
            raise GitLabAPIError("GitLab returned an invalid project list.")
        for project in page:
            project_id = project.get("id") if isinstance(project, dict) else None
            if type(project_id) is not int or project_id <= last_id:
                raise GitLabAPIError("GitLab returned invalid or unordered project IDs.")
            last_id = project_id
            projects.append(project)
        if len(page) < 100:
            return projects


def _fetch_issues(client: httpx.Client, project_id: int) -> list[dict[str, Any]]:
    """Fetch open and closed project issues, preserving their API payloads.

    Offset pagination supports older self-hosted GitLab releases as well.
    Errors abort the fetch rather than returning a partial issue inventory.
    """
    return _fetch_project_items(client, project_id, "issues")


def _fetch_merge_requests(client: httpx.Client, project_id: int) -> list[dict[str, Any]]:
    """Fetch project MRs in all states, including closed and merged MRs."""
    return _fetch_project_items(client, project_id, "merge_requests")


def _fetch_project_items(
    client: httpx.Client, project_id: int, resource: Literal["issues", "merge_requests"]
) -> list[dict[str, Any]]:
    """Read all pages of project issues or MRs, preserving their raw payloads."""
    items = []
    seen_ids = set()
    page_number = 1
    while True:
        page = _get_json(
            client,
            f"projects/{project_id}/{resource}",
            params={
                "scope": "all",
                "state": "all",
                "order_by": "created_at",
                "sort": "asc",
                "per_page": 100,
                "page": page_number,
            },
        )
        if not isinstance(page, list):
            raise GitLabAPIError(f"GitLab returned an invalid {resource} list.")
        for issue in page:
            if (
                not isinstance(issue, dict)
                or type(issue.get("id")) is not int
                or issue["id"] <= 0
                or type(issue.get("iid")) is not int
                or issue["iid"] <= 0
                or issue.get("project_id") != project_id
            ):
                raise GitLabAPIError(f"GitLab returned invalid {resource} identity fields.")
            if issue["id"] in seen_ids:
                raise GitLabAPIError(f"GitLab returned duplicate {resource} during pagination.")
            seen_ids.add(issue["id"])
            items.append(issue)
        if len(page) < 100:
            return items
        page_number += 1


def _fetch_wiki_pages(client: httpx.Client, project_id: int) -> list[dict[str, Any]]:
    """Fetch all project wiki pages with content; this endpoint is not paginated."""
    pages = _get_json(client, f"projects/{project_id}/wikis", params={"with_content": 1})
    if not isinstance(pages, list):
        raise GitLabAPIError("GitLab returned an invalid wiki page list.")
    seen_slugs = set()
    for page in pages:
        if (
            not isinstance(page, dict)
            or not isinstance(page.get("slug"), str)
            or not page["slug"].strip()
            or not isinstance(page.get("title"), str)
            or not isinstance(page.get("content"), str)
        ):
            raise GitLabAPIError("GitLab returned invalid wiki page fields.")
        if page["slug"] in seen_slugs:
            raise GitLabAPIError("GitLab returned duplicate wiki page slugs.")
        seen_slugs.add(page["slug"])
    return pages


def gitlab_source(
    token: str,
    *,
    resource_types: Sequence[ResourceType],
    base_url: str = "https://gitlab.com",
):
    """Create a GitLab document source for cognee (not yet implemented).

    Planned inputs:
        token: Required legacy PAT with read_api scope.
        base_url: GitLab instance URL, including self-hosted instances.
        resource_types: Required, non-empty selection of content types.

    Validates the PAT and discovers all accessible projects (including public
    projects), then fetches selected issues, merge requests, and wiki pages.
    Comments, document rendering, incremental state, and deletion reconciliation
    are not implemented yet.
    """
    if not resource_types:
        raise ValueError("Select at least one resource type.")
    if isinstance(resource_types, (str, bytes)) or any(
        resource_type not in ("issues", "merge_requests", "comments", "wiki")
        for resource_type in resource_types
    ):
        raise ValueError("Resource types must be issues, merge_requests, comments, or wiki.")

    if not isinstance(token, str) or not token.strip():
        raise ValueError("A non-empty GitLab token is required.")
    url = httpx.URL(base_url)
    if (
        url.scheme not in ("http", "https")
        or not url.host
        or url.userinfo
        or url.query
        or url.fragment
    ):
        raise ValueError("base_url must be an HTTP(S) instance URL without credentials or query.")
    api_url = f"{str(url).rstrip('/')}/api/v4/"
    with httpx.Client(
        base_url=api_url,
        headers={"PRIVATE-TOKEN": token},
        timeout=30.0,
        follow_redirects=False,
    ) as client:
        _validate_token(client)
        projects = _discover_projects(client)
        for project in projects:
            if "issues" in resource_types:
                _fetch_issues(client, project["id"])
            if "merge_requests" in resource_types:
                _fetch_merge_requests(client, project["id"])
            if "wiki" in resource_types:
                _fetch_wiki_pages(client, project["id"])

    raise NotImplementedError("GitLab connector scaffold: ingestion is not implemented yet.")
