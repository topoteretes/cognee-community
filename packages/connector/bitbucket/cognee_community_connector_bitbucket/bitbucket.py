"""Bitbucket Cloud data source for pull requests, comments, and wiki pages.

The connector yields documents through cognee's DLT document ingestion path.
It keeps stable keys and sync state in the DLT resource state, so unchanged
content is not emitted again and successfully detected source deletions become
DLT hard deletes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, urlparse

from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = logging.getLogger("bitbucket_connector")

BITBUCKET_SOURCE_NAME = "bitbucket"
BITBUCKET_TABLE_NAME = "bitbucket_documents"
_API_ROOT = "https://api.bitbucket.org/2.0"
_ALLOWED_CONTENT_TYPES = {"pull_requests", "comments", "wiki"}
_VALID_PR_STATES = ("OPEN", "MERGED", "DECLINED")
_PAGE_LENGTH = 50
_MAX_RETRIES = 5
_MAX_RETRY_DELAY = 60.0


class BitbucketAPIError(RuntimeError):
    """A safe, credential-free error returned by the Bitbucket API client."""


class _BitbucketClient:
    def __init__(self, session: Any, *, email: str | None, api_token: str | None):
        self.session = session
        if email and api_token:
            self.session.auth = (email, api_token)
        self.session.headers.update({"Accept": "application/json"})

    def _request(self, url: str, *, params: dict | None = None, raw: bool = False) -> Any:
        for attempt in range(_MAX_RETRIES):
            try:
                response = self.session.get(url, params=params or {}, timeout=30)
            except Exception as exc:
                if attempt == _MAX_RETRIES - 1 or not _is_network_error(exc):
                    raise BitbucketAPIError("Bitbucket request failed.") from None
                time.sleep(float(2**attempt))
                continue

            status = getattr(response, "status_code", None)
            if type(status) is not int:
                raise BitbucketAPIError("Bitbucket returned an invalid HTTP response.")
            if attempt < _MAX_RETRIES - 1 and (status in (408, 429, 500, 502, 503, 504)):
                time.sleep(_retry_after(getattr(response, "headers", {}), attempt))
                continue
            if status == 401:
                raise BitbucketAPIError(
                    "Bitbucket authentication failed. Check that the access token is valid "
                    "and not expired."
                )
            if status == 403:
                raise BitbucketAPIError(
                    "Bitbucket denied access. Check workspace/repository access and the "
                    "required read scopes."
                )
            if status == 404:
                raise BitbucketAPIError(
                    "A selected Bitbucket repository or resource was not found or is not "
                    "accessible."
                )
            if status == 429:
                raise BitbucketAPIError(
                    "Bitbucket rate limit remained exceeded after retries. Retry the sync later."
                )
            if status >= 400:
                raise BitbucketAPIError(f"Bitbucket returned HTTP {status}.")
            if raw:
                content = getattr(response, "content", None)
                if not isinstance(content, bytes):
                    raise BitbucketAPIError("Bitbucket returned an invalid raw file response.")
                return content
            try:
                payload = response.json()
            except Exception:
                raise BitbucketAPIError("Bitbucket returned an invalid JSON response.") from None
            if not isinstance(payload, dict):
                raise BitbucketAPIError("Bitbucket returned an unexpected response shape.")
            return payload
        raise BitbucketAPIError("Bitbucket request failed after retries.")

    def get(self, path_or_url: str, *, params: dict | None = None) -> dict:
        if path_or_url.startswith("http"):
            parsed = urlparse(path_or_url)
            if (
                parsed.scheme != "https"
                or parsed.hostname != "api.bitbucket.org"
                or parsed.port not in (None, 443)
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise BitbucketAPIError("Bitbucket returned an unsafe pagination URL.")
            url = path_or_url
        else:
            url = f"{_API_ROOT}/{path_or_url.lstrip('/')}"
        return self._request(url, params=params)

    def get_text(self, path: str) -> str | None:
        content = self._request(f"{_API_ROOT}/{path.lstrip('/')}", raw=True)
        if b"\x00" in content:
            return None
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            raise BitbucketAPIError(
                "Bitbucket returned a wiki file that is not valid UTF-8."
            ) from None


def _is_network_error(exc: Exception) -> bool:
    try:
        import requests
    except ImportError:  # pragma: no cover - requests is a declared dependency
        return False
    return isinstance(exc, requests.exceptions.RequestException)


def _retry_after(headers: Any, attempt: int) -> float:
    value = (headers or {}).get("Retry-After") or (headers or {}).get("retry-after")
    try:
        return min(_MAX_RETRY_DELAY, max(0.0, float(value)))
    except (TypeError, ValueError):
        if value:
            try:
                delay = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
                return min(_MAX_RETRY_DELAY, max(0.0, delay))
            except (TypeError, ValueError, OverflowError):
                pass
    return min(_MAX_RETRY_DELAY, float(2**attempt))


def _paginate(client: _BitbucketClient, path: str, params: dict | None = None) -> Iterator[dict]:
    """Yield Bitbucket Cloud's ``values`` entries, following opaque next URLs."""
    next_url: str | None = path
    next_params = dict(params or {})
    seen: set[str] = set()
    while next_url:
        if next_url in seen:
            raise BitbucketAPIError("Bitbucket pagination returned a repeated next-page link.")
        seen.add(next_url)
        payload = client.get(next_url, params=next_params)
        values = payload.get("values")
        if not isinstance(values, list):
            raise BitbucketAPIError("Bitbucket returned an unexpected paginated response shape.")
        next_link = payload.get("next")
        if next_link is not None and (not isinstance(next_link, str) or not next_link):
            raise BitbucketAPIError("Bitbucket returned an invalid pagination link.")
        size = payload.get("size")
        if size is not None and (type(size) is not int or size < 0):
            raise BitbucketAPIError("Bitbucket returned an invalid pagination size.")
        if not values and (next_link is not None or size):
            raise BitbucketAPIError("Bitbucket returned an incomplete empty page.")
        for item in values:
            if not isinstance(item, dict):
                raise BitbucketAPIError(
                    "Bitbucket returned a malformed item in a paginated response."
                )
            yield item
        next_url = next_link
        next_params = {}


def _digest(value: dict) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _iso_timestamp(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat(timespec="microseconds")
    except ValueError:
        return ""


def _author_name(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    return str(value.get("display_name") or value.get("nickname") or "")


def _html_or_raw_content(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    return str(value.get("raw") or value.get("html") or "")


def _pull_request_document(workspace: str, repo_slug: str, pull_request: dict) -> dict:
    number = pull_request.get("id")
    if number is None:
        raise BitbucketAPIError("Bitbucket returned a pull request without an id.")
    source = (pull_request.get("source") or {}).get("branch") or {}
    destination = (pull_request.get("destination") or {}).get("branch") or {}
    title = str(pull_request.get("title") or f"Pull request {number}")
    details = [
        f"Pull request: {title}",
        f"Repository: {workspace}/{repo_slug}",
        f"State: {pull_request.get('state') or 'UNKNOWN'}",
        f"Author: {_author_name(pull_request.get('author')) or 'Unknown'}",
        f"Source branch: {source.get('name') or 'Unknown'}",
        f"Destination branch: {destination.get('name') or 'Unknown'}",
        "",
        _html_or_raw_content(pull_request.get("description"))
        if isinstance(pull_request.get("description"), dict)
        else str(pull_request.get("description") or ""),
    ]
    repo = f"{workspace}/{repo_slug}"
    return {
        "id": f"{repo}:pullrequest:{number}",
        "title": title,
        "content": "\n".join(details).strip(),
        "url": ((pull_request.get("links") or {}).get("html") or {}).get("href") or "",
        "source_type": "pull_request",
        "updated_on": _iso_timestamp(pull_request.get("updated_on")),
    }


def _comment_document(workspace: str, repo_slug: str, pull_number: Any, comment: dict) -> dict:
    comment_id = comment.get("id")
    if comment_id is None:
        raise BitbucketAPIError("Bitbucket returned a pull request comment without an id.")
    repo = f"{workspace}/{repo_slug}"
    text = _html_or_raw_content(comment.get("content"))
    inline = comment.get("inline") or {}
    if inline.get("path"):
        text = f"File: {inline['path']}\n\n{text}"
    return {
        "id": f"{repo}:pullrequest:{pull_number}:comment:{comment_id}",
        "title": f"Comment on {repo} pull request {pull_number}",
        "content": text,
        "url": ((comment.get("links") or {}).get("html") or {}).get("href") or "",
        "source_type": "pull_request_comment",
        "updated_on": _iso_timestamp(comment.get("updated_on") or comment.get("created_on")),
    }


def _wiki_files(
    client: _BitbucketClient, workspace: str, repo_slug: str
) -> Iterator[tuple[str, str]]:
    """Read text files from the repo's separate Bitbucket Cloud wiki repository."""
    wiki_repo = f"{repo_slug}.wiki"
    pending = [""]
    visited: set[str] = set()
    while pending:
        directory = pending.pop()
        if directory in visited:
            continue
        visited.add(directory)
        path = f"repositories/{quote(workspace, safe='')}/{quote(wiki_repo, safe='')}/src/HEAD/"
        if directory:
            path += quote(directory, safe="/")
        for item in _paginate(client, path):
            item_path = item.get("path")
            item_type = item.get("type")
            if (
                not isinstance(item_path, str)
                or not item_path
                or item_path.startswith("/")
                or "\\" in item_path
                or "\x00" in item_path
                or any(part in ("", ".", "..") for part in item_path.split("/"))
            ):
                raise BitbucketAPIError("Bitbucket returned a malformed wiki file listing.")
            if item_type in ("commit_directory", "directory"):
                pending.append(item_path)
            elif item_type in ("commit_file", "file"):
                file_path = quote(item_path, safe="/")
                mime_type, _ = mimetypes.guess_type(item_path)
                if mime_type and not mime_type.startswith("text/"):
                    continue
                file_endpoint = (
                    f"repositories/{quote(workspace, safe='')}/{quote(wiki_repo, safe='')}"
                    f"/src/HEAD/{file_path}"
                )
                content = client.get_text(file_endpoint)
                if content is not None:
                    yield item_path, content
            else:
                raise BitbucketAPIError("Bitbucket returned a wiki entry with an unknown type.")


def _scope_key(workspace: str, repo_slug: str, content_type: str) -> str:
    return f"{workspace}/{repo_slug}:{content_type}"


def _sync_documents(
    client: _BitbucketClient,
    state: dict,
    *,
    workspace: str,
    repositories: list[str],
    content_types: set[str],
) -> list[dict]:
    """Fetch selected inventories completely, then append scoped tombstones."""
    previous_scopes = {
        key: set(identities) for key, identities in state.get("known_ids_by_scope", {}).items()
    }
    fingerprints = dict(state.get("fingerprints", {}))
    last_sync_by_repo = dict(state.get("last_sync_by_repo", {}))
    current_scopes: dict[str, set[str]] = {}
    pending_rows: dict[str, dict] = {}

    for repo_slug in repositories:
        repo_key = f"{workspace}/{repo_slug}"
        prefix = f"repositories/{quote(workspace, safe='')}/{quote(repo_slug, safe='')}"
        pull_path = f"{prefix}/pullrequests"
        inventory = (
            list(
                _paginate(
                    client,
                    pull_path,
                    {
                        "pagelen": _PAGE_LENGTH,
                        "state": list(_VALID_PR_STATES),
                    },
                )
            )
            if {"pull_requests", "comments"} & content_types
            else []
        )
        inventory_by_id: dict[str, dict] = {}
        for item in inventory:
            if item.get("id") is None:
                raise BitbucketAPIError("Bitbucket returned a pull request without an id.")
            inventory_by_id[str(item["id"])] = item

        if "pull_requests" in content_types:
            scope = _scope_key(workspace, repo_slug, "pull_requests")
            scope_ids: set[str] = set()
            current_scopes[scope] = scope_ids
            previous_pr_ids = {
                identity.rsplit(":pullrequest:", 1)[-1]
                for identity in previous_scopes.get(scope, set())
            }
            for pr_id in inventory_by_id:
                identity = f"{workspace}/{repo_slug}:pullrequest:{pr_id}"
                scope_ids.add(identity)
                # The lightweight inventory sweep is sufficient to preserve
                # unchanged records. Changed records below replace their hash.
            # Older global cursors are intentionally ignored. Replaying one full
            # repo snapshot is safer than applying another repo's high-water mark.
            repo_cursor = str(last_sync_by_repo.get(repo_key, ""))
            if repo_cursor:
                query = f'updated_on >= "{repo_cursor}"'
                changed_prs = _paginate(
                    client,
                    pull_path,
                    {"pagelen": _PAGE_LENGTH, "state": list(_VALID_PR_STATES), "q": query},
                )
            else:
                changed_prs = _paginate(
                    client,
                    pull_path,
                    {"pagelen": _PAGE_LENGTH, "state": list(_VALID_PR_STATES)},
                )
            changed_by_id: dict[str, dict] = {}
            for pull_request in changed_prs:
                if pull_request.get("id") is None:
                    raise BitbucketAPIError("Bitbucket returned a pull request without an id.")
                pr_id = str(pull_request["id"])
                # Filter results must still exist in the current selected repo.
                if pr_id not in inventory_by_id:
                    continue
                changed_by_id[pr_id] = pull_request

            # A newly visible PR can have an old updated_on value (for example,
            # after access is granted). Fetch it by ID so the cursor cannot hide it.
            if repo_cursor:
                for pr_id in inventory_by_id.keys() - previous_pr_ids:
                    detail_path = f"{pull_path}/{quote(pr_id, safe='')}"
                    pull_request = client.get(detail_path)
                    if pull_request.get("id") is None:
                        raise BitbucketAPIError("Bitbucket returned a pull request without an id.")
                    changed_by_id[pr_id] = pull_request

            newest_repo_cursor = repo_cursor
            for inventory_item in inventory_by_id.values():
                item_timestamp = _iso_timestamp(inventory_item.get("updated_on"))
                newest_repo_cursor = max(newest_repo_cursor, item_timestamp)

            for pull_request in changed_by_id.values():
                doc = _pull_request_document(workspace, repo_slug, pull_request)
                identity = doc["id"]
                fingerprint = _digest(doc)
                if fingerprints.get(identity) != fingerprint:
                    pending_rows[identity] = doc
                fingerprints[identity] = fingerprint
            last_sync_by_repo[repo_key] = newest_repo_cursor

        # PR inventory is also the parent list used for comment deletion checks.
        if "comments" in content_types:
            scope = _scope_key(workspace, repo_slug, "comments")
            scope_ids = set()
            current_scopes[scope] = scope_ids
            for pull_number in sorted(inventory_by_id, key=lambda value: int(value)):
                comment_path = f"{pull_path}/{quote(pull_number, safe='')}/comments"
                seen_comments: set[str] = set()
                for comment in _paginate(client, comment_path, {"pagelen": _PAGE_LENGTH}):
                    # Bitbucket retains deleted comments as tombstone-shaped
                    # objects in the comments API. Treat them as absent so an
                    # existing Cognee document is hard-deleted rather than
                    # replaced with an empty comment document.
                    if comment.get("deleted") is True:
                        continue
                    doc = _comment_document(workspace, repo_slug, pull_number, comment)
                    identity = doc["id"]
                    if identity in seen_comments:
                        continue
                    seen_comments.add(identity)
                    scope_ids.add(identity)
                    fingerprint = _digest(doc)
                    if fingerprints.get(identity) != fingerprint:
                        pending_rows[identity] = doc
                    fingerprints[identity] = fingerprint

        if "wiki" in content_types:
            scope = _scope_key(workspace, repo_slug, "wiki")
            repo_meta = client.get(prefix)
            if "has_wiki" not in repo_meta:
                # Bitbucket may omit false/unavailable capability fields from
                # repository metadata. Preserve the last wiki inventory rather
                # than interpreting an unknown status as a complete empty wiki.
                continue
            has_wiki = repo_meta.get("has_wiki")
            if not isinstance(has_wiki, bool):
                raise BitbucketAPIError("Bitbucket returned an invalid has_wiki value.")

            scope_ids = set()
            current_scopes[scope] = scope_ids
            if has_wiki:
                for page_path, content in _wiki_files(client, workspace, repo_slug):
                    identity = f"{workspace}/{repo_slug}:wiki:{page_path}"
                    doc = {
                        "id": identity,
                        "title": page_path.rsplit("/", 1)[-1],
                        "content": content,
                        "url": (
                            f"https://bitbucket.org/{workspace}/{repo_slug}/wiki/"
                            f"{quote(page_path, safe='/')}"
                        ),
                        "source_type": "wiki_page",
                        "updated_on": "",
                    }
                    fingerprint = _digest(doc)
                    scope_ids.add(identity)
                    if fingerprints.get(identity) != fingerprint:
                        pending_rows[identity] = doc
                    fingerprints[identity] = fingerprint

    deleted_ids: set[str] = set()
    for scope, current_ids in current_scopes.items():
        deleted_ids.update(previous_scopes.get(scope, set()) - current_ids)
        for identity in previous_scopes.get(scope, set()) - current_ids:
            fingerprints.pop(identity, None)

    all_scopes = {**previous_scopes, **current_scopes}
    rows = list(pending_rows.values())
    rows.extend({"id": identity, "_deleted": True} for identity in sorted(deleted_ids))

    # Advance state only after every selected repository and resource completed.
    state["known_ids_by_scope"] = {
        key: sorted(identities) for key, identities in all_scopes.items()
    }
    state["fingerprints"] = fingerprints
    state["last_sync_by_repo"] = last_sync_by_repo
    logger.info(
        "Bitbucket: %d changed document(s), %d deletion(s).", len(pending_rows), len(deleted_ids)
    )
    return rows


def bitbucket_source(
    *,
    workspace: str,
    repositories: list[str],
    content_types: list[str] | None = None,
    access_token: str | None = None,
    api_token: str | None = None,
    email: str | None = None,
    session: Any = None,
):
    """Create a DLT source for selected Bitbucket Cloud repository content.

    Args:
        workspace: Bitbucket Cloud workspace slug.
        repositories: Repository slugs to sync in that workspace.
        content_types: Any of ``pull_requests``, ``comments``, and ``wiki``;
            defaults to all three.
        access_token: OAuth 2.0 bearer access token (or ``BITBUCKET_ACCESS_TOKEN``).
        api_token: Atlassian API token (or ``BITBUCKET_API_TOKEN``). Use with
            ``email`` / ``BITBUCKET_EMAIL`` for Basic authentication.
        email: Atlassian account email used with ``api_token``.
        session: Optional preconfigured requests-compatible session, mainly for tests.

    Returns:
        A DLT source that can be passed to ``cognee.remember`` or ``cognee.add``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            "The Bitbucket connector requires dlt. Install the package extra with "
            '`pip install "cognee[bitbucket]"`.'
        ) from exc

    if not workspace or not workspace.strip():
        raise ValueError("bitbucket_source requires a workspace slug.")
    if not repositories or any(not repo.strip() for repo in repositories):
        raise ValueError("bitbucket_source requires one or more repository slugs.")
    selected = set(_ALLOWED_CONTENT_TYPES if content_types is None else content_types)
    invalid = selected - _ALLOWED_CONTENT_TYPES
    if invalid:
        raise ValueError(f"Unsupported Bitbucket content type(s): {', '.join(sorted(invalid))}.")
    if not selected:
        raise ValueError("bitbucket_source requires at least one content type.")

    if access_token:
        resolved_access_token = access_token
        resolved_api_token = resolved_email = None
    elif api_token or email:
        resolved_access_token = None
        resolved_api_token = api_token
        resolved_email = email
    else:
        resolved_access_token = os.environ.get("BITBUCKET_ACCESS_TOKEN")
        resolved_api_token = os.environ.get("BITBUCKET_API_TOKEN")
        resolved_email = os.environ.get("BITBUCKET_EMAIL")
    if session is None and not (resolved_access_token or (resolved_api_token and resolved_email)):
        raise ValueError(
            "Bitbucket credentials required: pass access_token= (OAuth 2.0) or "
            "api_token= and email=, or set BITBUCKET_ACCESS_TOKEN / BITBUCKET_API_TOKEN "
            "and BITBUCKET_EMAIL."
        )
    if session is None:
        try:
            import requests
        except ImportError as exc:  # pragma: no cover - declared dependency
            raise ImportError('The Bitbucket connector requires "requests".') from exc
        session = requests.Session()
        if resolved_access_token:
            session.headers["Authorization"] = f"Bearer {resolved_access_token}"
    elif resolved_access_token:
        session.headers["Authorization"] = f"Bearer {resolved_access_token}"
    elif resolved_api_token and resolved_email:
        session.auth = (resolved_email, resolved_api_token)
    client = _BitbucketClient(session, email=resolved_email, api_token=resolved_api_token)
    normalized_repositories = list(dict.fromkeys(repo.strip() for repo in repositories))

    @dlt.resource(
        name=BITBUCKET_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def bitbucket_documents():
        resource_state = dlt.current.resource_state()
        yield from _sync_documents(
            client,
            resource_state,
            workspace=workspace.strip(),
            repositories=normalized_repositories,
            content_types=selected,
        )

    @dlt.source(name=BITBUCKET_SOURCE_NAME)
    def _bitbucket():
        return bitbucket_documents

    source = _bitbucket()
    setattr(source, DOCUMENT_SOURCE_ATTR, BITBUCKET_SOURCE_NAME)
    return source
