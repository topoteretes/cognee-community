"""Fetch GitLab resources and expose mapped documents as a DLT source."""

import json
import logging
from collections.abc import Sequence
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote

import httpx

from .documents import _instance_url

if TYPE_CHECKING:
    from dlt.extract.source import DltSource

ResourceType = Literal["issues", "merge_requests", "comments", "wiki"]

logger = logging.getLogger(__name__)


class GitLabAPIError(RuntimeError):
    """GitLab could not complete a required read; never treat this as empty data."""


def _get_json(
    client: httpx.Client,
    path: str,
    *,
    missing_ok: bool = False,
    forbidden_ok: bool = False,
    **kwargs,
) -> Any:
    """Make a GitLab GET request and parse its successful JSON response.

    ``missing_ok`` maps 404 to None; ``forbidden_ok`` maps 403 to None as well.
    """
    try:
        response = client.get(path, **kwargs)
    except httpx.RequestError:
        raise GitLabAPIError("GitLab request failed: network error or timeout.") from None

    if response.status_code == 401:
        raise GitLabAPIError("GitLab rejected the token: invalid, expired, or revoked.")
    if response.status_code == 403 and forbidden_ok:
        return None
    if response.status_code == 403:
        raise GitLabAPIError("GitLab denied access. Check read_api scope and project permissions.")
    if response.status_code == 404 and missing_ok:
        return None
    if response.status_code == 429:
        raise GitLabAPIError("GitLab rate limit reached. Retry the sync later.")
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
    """List projects the token owner is a member of, using keyset pagination.

    ``membership=true`` covers personal projects and projects shared through
    groups, but excludes unrelated public projects on the instance. Advance by
    project ID rather than following server-supplied URLs, so the credential
    stays on the configured instance. Failed pages abort discovery.
    """
    projects = []
    last_id = 0
    while True:
        params = {
            "membership": "true",
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


def _read_selected_projects(
    client: httpx.Client, project_ids: Sequence[int | str]
) -> list[dict[str, Any]]:
    """Read explicitly selected projects by numeric ID or ``group/project`` path.

    A selected project that is missing or no longer visible (404) is left out,
    so documents synced from it earlier are removed. Any other failure aborts.
    """
    projects = {}
    for reference in project_ids:
        project = _get_json(client, f"projects/{quote(str(reference), safe='')}", missing_ok=True)
        if project is None:
            logger.warning("GitLab project %r is not accessible; skipping it.", reference)
            continue
        project_id = project.get("id") if isinstance(project, dict) else None
        if (
            type(project_id) is not int
            or project_id <= 0
            or (type(reference) is int and project_id != reference)
        ):
            raise GitLabAPIError("GitLab returned invalid project identity for a selection.")
        # A numeric ID and a path can name the same project; sync it once.
        projects[project_id] = project
    return list(projects.values())


def _feature_enabled(project: dict[str, Any], resource: str) -> bool:
    """Whether project metadata leaves a resource on; absent fields mean enabled.

    Newer GitLab reports ``<feature>_access_level``; older releases report
    ``<feature>_enabled``. Either one marking the feature off disables it.
    """
    return (
        project.get(f"{resource}_access_level") != "disabled"
        and project.get(f"{resource}_enabled") is not False
    )


def _fetch_issues(
    client: httpx.Client,
    project_id: int,
    *,
    updated_after: str | None = None,
    unavailable_ok: bool = False,
) -> list[dict[str, Any]] | None:
    """Fetch open and closed project issues, preserving their API payloads.

    Offset pagination supports older self-hosted GitLab releases as well.
    Errors abort the fetch rather than returning a partial issue inventory.
    With ``unavailable_ok``, a 403/404 on the first page returns None.
    """
    return _fetch_project_items(
        client,
        project_id,
        "issues",
        updated_after=updated_after,
        unavailable_ok=unavailable_ok,
    )


def _fetch_merge_requests(
    client: httpx.Client,
    project_id: int,
    *,
    updated_after: str | None = None,
    unavailable_ok: bool = False,
) -> list[dict[str, Any]] | None:
    """Fetch project MRs in all states, including closed and merged MRs."""
    return _fetch_project_items(
        client,
        project_id,
        "merge_requests",
        updated_after=updated_after,
        unavailable_ok=unavailable_ok,
    )


def _fetch_project_items(
    client: httpx.Client,
    project_id: int,
    resource: Literal["issues", "merge_requests"],
    *,
    updated_after: str | None = None,
    unavailable_ok: bool = False,
) -> list[dict[str, Any]] | None:
    """Read all pages of project issues or MRs, preserving their raw payloads.

    Only the first page may report the feature as unavailable: a 403/404 after
    pages were read means an incomplete inventory, so it still aborts.
    """
    items = []
    seen_ids = set()
    page_number = 1
    while True:
        filters = {"updated_after": updated_after} if updated_after is not None else {}
        first_page_optional = unavailable_ok and page_number == 1
        page = _get_json(
            client,
            f"projects/{project_id}/{resource}",
            missing_ok=first_page_optional,
            forbidden_ok=first_page_optional,
            params={
                "scope": "all",
                "state": "all",
                "order_by": "created_at",
                "sort": "asc",
                "per_page": 100,
                "page": page_number,
                **filters,
            },
        )
        if page is None and first_page_optional:
            return None
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


def _fetch_wiki_pages(
    client: httpx.Client, project_id: int, *, unavailable_ok: bool = False
) -> list[dict[str, Any]] | None:
    """Fetch all project wiki pages with content; this endpoint is not paginated.

    With ``unavailable_ok``, a 403/404 (wiki turned off) returns None.
    """
    pages = _get_json(
        client,
        f"projects/{project_id}/wikis",
        missing_ok=unavailable_ok,
        forbidden_ok=unavailable_ok,
        params={"with_content": 1},
    )
    if pages is None and unavailable_ok:
        return None
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


def _normalize_project_ids(
    project_ids: Sequence[int | str] | None,
) -> tuple[int | str, ...] | None:
    """Validate a project selection; None means membership discovery."""
    if project_ids is None:
        return None
    if isinstance(project_ids, (str, bytes, int)) or not project_ids:
        raise ValueError("project_ids must be a non-empty list of project IDs or paths.")
    refs = []
    for ref in project_ids:
        if type(ref) is int and ref > 0:
            refs.append(ref)
        elif isinstance(ref, str) and ref.strip().strip("/"):
            refs.append(ref.strip().strip("/"))
        else:
            raise ValueError("Each project_ids entry must be a positive ID or a project path.")
    return tuple(dict.fromkeys(refs))


def gitlab_source(
    token: str,
    *,
    resource_types: Sequence[ResourceType],
    base_url: str = "https://gitlab.com",
    project_ids: Sequence[int | str] | None = None,
) -> "DltSource":
    """Fetch selected GitLab content and return a document-mode DLT source.

    Args:
        token: Required legacy PAT with read_api scope.
        base_url: GitLab instance URL, including self-hosted instances.
        resource_types: Required, non-empty selection of content types.
        project_ids: Optional projects to sync, as numeric IDs or
            ``group/project`` paths. When omitted, every project the token
            owner is a member of (directly or through a group) is synced.

    Validates the PAT immediately. Resource fetching runs during DLT extraction,
    using per-project checkpoints in DLT resource state. Pass
    write_disposition="merge" and max_rows_per_table=0 to cognee.remember().
    Comments are not implemented and selecting them raises before HTTP calls.
    Projects with issues, MRs, or the wiki turned off skip that resource only.
    """
    if not resource_types:
        raise ValueError("Select at least one resource type.")
    if isinstance(resource_types, (str, bytes)) or any(
        resource_type not in ("issues", "merge_requests", "comments", "wiki")
        for resource_type in resource_types
    ):
        raise ValueError("Resource types must be issues, merge_requests, comments, or wiki.")
    if "comments" in resource_types:
        raise NotImplementedError("Comment ingestion is not implemented yet.")
    selected = tuple(sorted(set(resource_types)))
    project_refs = _normalize_project_ids(project_ids)

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
    instance = _instance_url(str(url))
    client_options = {
        "base_url": api_url,
        "headers": {"PRIVATE-TOKEN": token},
        "timeout": 30.0,
        "follow_redirects": False,
    }
    with httpx.Client(**client_options) as client:
        metadata = _validate_token(client)
        owner_id = metadata.get("user_id")
        if type(owner_id) is not int or owner_id <= 0:
            raise GitLabAPIError(
                "GitLab token metadata requires a valid user_id for source isolation."
            )

    import dlt
    from cognee.tasks.ingestion import dlt_utils

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError("GitLab document ingestion requires DOCUMENT_SYNC_VERSION >= 1.")
    # Exclude credentials from identity so rotating the PAT preserves staging.
    # A project selection is its own scope, so narrowing it never deletes
    # documents another selection still owns.
    identity = [instance, owner_id, selected]
    if project_refs is not None:
        identity.append(sorted(str(ref) for ref in project_refs))
    scope = sha256(json.dumps(identity).encode()).hexdigest()[:32]
    resource_name = f"gitlab_documents_{scope}"

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns={
            **{name: {"data_type": "text"} for name in ("id", "title", "content", "url")},
            "_deleted": {"data_type": "bool", "hard_delete": True},
        },
    )
    def gitlab_documents():
        from .sync import sync_documents

        with httpx.Client(**client_options) as client:
            # A source can be extracted long after construction: recheck auth.
            if _validate_token(client).get("user_id") != owner_id:
                raise GitLabAPIError("GitLab token owner changed before extraction.")
            yield from sync_documents(
                client,
                instance,
                selected,
                dlt.current.resource_state(),
                project_ids=project_refs,
            )

    @dlt.source(name=f"gitlab_{scope}")
    def source_factory():
        return gitlab_documents

    source = source_factory()
    setattr(source, dlt_utils.DOCUMENT_SOURCE_ATTR, "gitlab")
    setattr(source, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    return source
