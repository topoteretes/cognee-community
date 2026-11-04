"""Slite Data-Source Connector for Cognee.

Syncs team documentation, runbooks, and policies from Slite into Cognee memory.
Preserves Slite review state (Verified, Outdated, VerificationRequested, VerificationExpired),
supports parent note subtree selection or full workspace sync, incremental change detection,
and safe forget-on-delete.
Resolves issue: topoteretes/cognee#6153.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import requests

logger = logging.getLogger("cognee_community_connector_slite")

BASE_URL = "https://api.slite.com/v1"
DEFAULT_PAGE_SIZE = 50
DEFAULT_MAX_REQUESTS = 500
_TIMEOUT = (10, 30)
_RETRIES = 3

# Optional dlt / cognee imports for ecosystem compatibility
try:
    import dlt
    from cognee.tasks.ingestion import dlt_utils
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
    _has_dlt = True
except ImportError:
    dlt = None
    dlt_utils = None
    DOCUMENT_SOURCE_ATTR = "document_source"
    _has_dlt = False


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class SliteSourceError(RuntimeError):
    """Base exception for Slite connector errors."""


class SliteAPIError(SliteSourceError):
    """Slite answered with an unhandled HTTP error or could not be reached."""


class SliteAuthError(SliteSourceError):
    """Slite rejected the API key (HTTP 401)."""


class SliteAccessError(SliteSourceError):
    """The API key does not have permission to access the resource (HTTP 403)."""


class SliteRateLimitedError(SliteSourceError):
    """Slite API rate limit exceeded (HTTP 429)."""


class SliteNotFoundError(SliteAPIError):
    """The requested note or resource does not exist (HTTP 404)."""


class SlitePayloadTooLargeError(SliteAPIError):
    """The requested note body exceeds maximum payload size (HTTP 413)."""


class SliteValidationError(SliteAPIError):
    """Slite request validation failed (HTTP 422)."""


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
def _hash(val: Any) -> str:
    """Deterministic hash of structured data."""
    encoded = json.dumps(val, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _extract_channel(note: dict[str, Any]) -> str | None:
    """Extract top-level channel or folder name from note path or parent notes."""
    parent_notes = note.get("parentNotes")
    if isinstance(parent_notes, list) and parent_notes:
        first = parent_notes[0]
        if isinstance(first, dict):
            return first.get("title") or first.get("name")
        elif isinstance(first, str):
            return first
    channel = note.get("channel") or note.get("channelTitle")
    if isinstance(channel, dict):
        return channel.get("title") or channel.get("name")
    if isinstance(channel, str) and channel.strip():
        return channel.strip()
    return None


def render_note(
    note: dict[str, Any],
    body_markdown: str | None = None,
    channel_name: str | None = None,
) -> dict[str, Any]:
    """Format a Slite note into a standardized document for Cognee.
    
    Includes review state, channel node sets, and markdown content.
    """
    note_id = str(note.get("id") or "")
    title = str(note.get("title") or "Untitled Note").strip()
    url = str(note.get("url") or f"https://slite.com/app/docs/{note_id}").strip()
    review_state = note.get("reviewState") or None
    owner = note.get("owner")
    if isinstance(owner, dict):
        owner_name = owner.get("name") or owner.get("email") or str(owner.get("id") or "")
    else:
        owner_name = str(owner) if owner is not None else ""

    channel = channel_name or _extract_channel(note) or "General"
    last_edited_at = note.get("lastEditedAt") or note.get("updatedAt") or ""
    created_at = note.get("createdAt") or ""
    updated_at = note.get("updatedAt") or ""
    archived_at = note.get("archivedAt")

    content = body_markdown if body_markdown is not None else note.get("content") or ""

    node_sets = [f"slite:channel:{channel}"]
    if review_state:
        node_sets.append(f"slite:review:{review_state}")

    text_parts = [
        f"Title: {title}",
        f"Channel: {channel}",
        f"Last Edited: {last_edited_at}",
    ]
    if review_state:
        text_parts.append(f"Review State: {review_state}")
    if owner_name:
        text_parts.append(f"Owner: {owner_name}")
    text_parts.append("")
    text_parts.append(content)

    return {
        "id": f"slite:note:{note_id}",
        "raw_note_id": note_id,
        "title": title,
        "url": url,
        "review_state": review_state,
        "owner": owner_name,
        "channel": channel,
        "created_at": created_at,
        "updated_at": updated_at,
        "last_edited_at": last_edited_at,
        "archived_at": archived_at,
        "node_sets": node_sets,
        "content": content,
        "text": "\n".join(text_parts),
        "_deleted": False,
    }


# ---------------------------------------------------------------------------
# Slite REST API Client
# ---------------------------------------------------------------------------
class SliteClient:
    """Client for the Slite v1 REST API."""

    def __init__(
        self,
        api_key: str,
        base_url: str = BASE_URL,
        sleep: Callable[[float], None] = time.sleep,
        timeout: tuple[int, int] = _TIMEOUT,
    ):
        key = (api_key or "").strip()
        if not key or not key.isascii() or not key.isprintable() or " " in key:
            raise ValueError("The Slite API key is empty or has an invalid format")
        self._api_key = key
        self.base_url = base_url.rstrip("/")
        self._sleep = sleep
        self._timeout = timeout
        self.rate_limit_remaining: int | None = None

    def __repr__(self) -> str:
        return f"SliteClient(base_url='{self.base_url}', api_key=<redacted>)"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "x-slite-api-key": self._api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "SliteCogneeConnector/1.0 (https://github.com/topoteretes/cognee-community)",
        }

    def request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
    ) -> Any:
        path = endpoint.lstrip("/")
        url = f"{self.base_url}/{path}"
        headers = self._headers()

        for attempt in range(_RETRIES):
            last = attempt == _RETRIES - 1
            try:
                response = requests.request(
                    method=method.upper(),
                    url=url,
                    params=params,
                    json=json_data,
                    headers=headers,
                    timeout=self._timeout,
                )
            except requests.RequestException as exc:
                if last:
                    raise SliteAPIError(f"Slite request failed: {type(exc).__name__}") from None
                self._sleep(2**attempt)
                continue

            if response.status_code >= 500 and not last:
                self._sleep(2**attempt)
                continue

            return self._parse_response(response, endpoint)

    def _parse_response(self, response: requests.Response, endpoint: str) -> Any:
        status = response.status_code
        if status == 401:
            raise SliteAuthError("Slite rejected the API key (HTTP 401: auth/unauthorized)")
        if status == 403:
            raise SliteAccessError(f"Access forbidden for Slite endpoint {endpoint} (HTTP 403)")
        if status == 404:
            raise SliteNotFoundError(f"Slite resource not found at {endpoint} (HTTP 404)")
        if status == 413:
            raise SlitePayloadTooLargeError(f"Slite note body is too large (HTTP 413)")
        if status == 422:
            raise SliteValidationError(f"Slite field validation error (HTTP 422)")
        if status == 429:
            raise SliteRateLimitedError("Slite API rate limit exceeded (HTTP 429: rate-limit)")
        if status not in (200, 201):
            raise SliteAPIError(f"Slite API error HTTP {status} for {endpoint}")

        # Update rate limits if present in headers
        remaining = response.headers.get("x-ratelimit-remaining") or response.headers.get("X-RateLimit-Remaining")
        if remaining is not None:
            try:
                self.rate_limit_remaining = int(remaining)
            except ValueError:
                pass

        try:
            return response.json()
        except ValueError:
            return {"text": response.text}

    def get_me(self) -> dict[str, Any]:
        """Fetch authenticated user and team identity."""
        return self.request("GET", "me")

    def get_notes(
        self,
        parent_note_id: str | None = None,
        order_by: str = "lastEditedAt_DESC",
        cursor: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        """List notes at workspace root or under a parent note."""
        params: dict[str, Any] = {"orderBy": order_by, "limit": limit}
        if parent_note_id:
            params["parentNoteId"] = parent_note_id
        if cursor:
            params["cursor"] = cursor
        return self.request("GET", "notes", params=params)

    def get_note_children(
        self,
        note_id: str,
        include_descendants: bool = True,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Fetch children of a note, optionally flattened to the full subtree."""
        params: dict[str, Any] = {"includeDescendants": "true" if include_descendants else "false"}
        if cursor:
            params["cursor"] = cursor
        return self.request("GET", f"notes/{note_id}/children", params=params)

    def get_note(self, note_id: str, format: str = "md") -> dict[str, Any]:
        """Retrieve full note metadata and content in specified format (md)."""
        return self.request("GET", f"notes/{note_id}", params={"format": format})

    def search_notes(
        self,
        parent_note_id: str | None = None,
        last_edited_after: str | None = None,
        last_updated_after: str | None = None,
        include_archived: bool = False,
        page: int = 0,
        hits_per_page: int = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        """Search notes with incremental timestamp filters."""
        params: dict[str, Any] = {
            "page": page,
            "hitsPerPage": min(hits_per_page, 100),
            "includeArchived": "true" if include_archived else "false",
        }
        if parent_note_id:
            params["parentNoteId"] = parent_note_id
        if last_edited_after:
            params["lastEditedAfter"] = last_edited_after
        if last_updated_after:
            params["lastUpdatedAfter"] = last_updated_after
        return self.request("GET", "search-notes", params=params)


# ---------------------------------------------------------------------------
# Workspace Walker & Change Detection
# ---------------------------------------------------------------------------
class _SliteWalker:
    """Orchestrates note discovery, change detection, body fetching, and deletion detection."""

    def __init__(
        self,
        client: Any,
        state: dict[str, Any],
        stats: dict[str, int],
        parent_note_ids: list[str] | None = None,
        include_archived: bool = False,
        max_requests: int = DEFAULT_MAX_REQUESTS,
    ):
        self.client = client
        self.state = state
        self.stats = stats
        self.parent_note_ids = list(parent_note_ids or [])
        self.include_archived = include_archived
        self.max_requests = max_requests

        # Persistent state: fingerprints maps note_id -> hash of metadata
        self.fingerprints: dict[str, str] = self.state.setdefault("fingerprints", {})
        self.initial_owner_id = self.state.get("authenticated_owner_id")
        self.seen_note_ids: set[str] = set()

    def _verify_owner_safety(self) -> None:
        """Safety check: ensure API key identity hasn't changed before performing tombstones."""
        try:
            me_data = self.client.get_me()
        except Exception as exc:
            logger.warning("Could not verify Slite /me identity; skipping deletions for safety: %s", exc)
            raise SliteSourceError(f"Slite /me verification failed: {exc}") from exc

        current_owner_id = (
            me_data.get("user", {}).get("id")
            or me_data.get("team", {}).get("id")
            or me_data.get("id")
        )
        if not current_owner_id:
            logger.warning("Slite /me response did not contain user/team id")
            return

        if self.initial_owner_id is None:
            self.state["authenticated_owner_id"] = current_owner_id
            self.initial_owner_id = current_owner_id
        elif self.initial_owner_id != current_owner_id:
            raise SliteAuthError(
                f"Slite API key owner changed from {self.initial_owner_id} to {current_owner_id}; "
                "aborting sync to prevent erroneous deletions."
            )

    def _list_all_candidate_notes(self) -> Iterator[tuple[dict[str, Any], str | None]]:
        """List note metadata across selected parent subtrees or whole workspace."""
        if self.parent_note_ids:
            for parent_id in self.parent_note_ids:
                # 1. Fetch parent note itself
                try:
                    parent_note = self.client.get_note(parent_id)
                    parent_title = parent_note.get("title") or "Parent"
                    yield parent_note, parent_title
                except SliteNotFoundError:
                    logger.warning("Selected parent note %s not found in Slite", parent_id)
                    continue

                # 2. Fetch children flattened subtree
                cursor = None
                has_next = True
                while has_next:
                    resp = self.client.get_note_children(
                        parent_id, include_descendants=True, cursor=cursor
                    )
                    notes = resp.get("notes") or []
                    for n in notes:
                        yield n, parent_title
                    has_next = resp.get("hasNextPage", False)
                    cursor = resp.get("nextCursor")
                    if not cursor:
                        break
        else:
            cursor = None
            has_next = True
            while has_next:
                resp = self.client.get_notes(cursor=cursor)
                notes = resp.get("notes") or []
                for n in notes:
                    yield n, None
                has_next = resp.get("hasNextPage", False)
                cursor = resp.get("nextCursor")
                if not cursor:
                    break

    def rows(self) -> Iterator[dict[str, Any]]:
        self.stats.update(scanned=0, fetched=0, skipped_unchanged=0, skipped_large=0, deleted=0, failed=0)

        # 1. Verify authenticated user identity for safety
        self._verify_owner_safety()

        # 2. Iterate through candidate notes and fetch changed content
        for note, channel in self._list_all_candidate_notes():
            note_id = str(note.get("id") or "")
            if not note_id:
                continue

            self.stats["scanned"] += 1
            self.seen_note_ids.add(note_id)

            # Check if archived
            archived_at = note.get("archivedAt")
            if archived_at and not self.include_archived:
                # Note is archived and not wanted in memory
                if note_id in self.fingerprints:
                    self.fingerprints.pop(note_id, None)
                    self.stats["deleted"] += 1
                    yield {"id": f"slite:note:{note_id}", "_deleted": True}
                continue

            # Compute change fingerprint
            metadata_fingerprint = _hash({
                "title": note.get("title"),
                "review_state": note.get("reviewState"),
                "last_edited_at": note.get("lastEditedAt"),
                "updated_at": note.get("updatedAt"),
                "archived_at": archived_at,
            })

            # Check if unchanged
            if self.fingerprints.get(note_id) == metadata_fingerprint:
                self.stats["skipped_unchanged"] += 1
                continue

            # Fetch Markdown body
            body_md = None
            try:
                note_detail = self.client.get_note(note_id, format="md")
                body_md = note_detail.get("content") or note_detail.get("body") or ""
                self.stats["fetched"] += 1
            except SlitePayloadTooLargeError:
                logger.warning("Slite note %s payload too large (413), retaining metadata without body", note_id)
                self.stats["skipped_large"] += 1
                body_md = f"*Note {note.get('title', note_id)} content too large to sync via API (HTTP 413)*"
            except SliteNotFoundError:
                # Deleted between listing and body fetch
                continue
            except Exception as exc:
                self.stats["failed"] += 1
                logger.warning("Failed to fetch body for Slite note %s: %s", note_id, exc)
                continue

            # Render document and record fingerprint
            doc = render_note(note, body_markdown=body_md, channel_name=channel)
            self.fingerprints[note_id] = metadata_fingerprint
            yield doc

        # 3. Detect and emit deletions for missing notes
        previously_seen = set(self.fingerprints.keys())
        missing = previously_seen - self.seen_note_ids
        for missing_id in missing:
            self.fingerprints.pop(missing_id, None)
            self.stats["deleted"] += 1
            yield {"id": f"slite:note:{missing_id}", "_deleted": True}


def _iter_notes(
    client: Any,
    state: dict[str, Any],
    stats: dict[str, int],
    parent_note_ids: list[str] | None = None,
    include_archived: bool = False,
    max_requests: int = DEFAULT_MAX_REQUESTS,
) -> Iterator[dict[str, Any]]:
    """Yield changed notes and deletion tombstones for a Slite workspace.
    
    Pure generator independent of dlt, suitable for unit and integration testing.
    """
    walker = _SliteWalker(
        client=client,
        state=state,
        stats=stats,
        parent_note_ids=parent_note_ids,
        include_archived=include_archived,
        max_requests=max_requests,
    )
    yield from walker.rows()


# ---------------------------------------------------------------------------
# Public dlt / Cognee Source Factory
# ---------------------------------------------------------------------------
def slite_source(
    *,
    resource_name: str,
    api_key: str | None = None,
    service: Any = None,
    parent_note_ids: list[str] | None = None,
    include_archived: bool = False,
    max_requests: int = DEFAULT_MAX_REQUESTS,
):
    """Create a dlt resource that syncs a Slite workspace or parent note subtrees into Cognee.

    Args:
        resource_name: Stable connection name for this Slite workspace (scopes sync state and tombstones).
        api_key: Personal Slite API key from Settings > API.
        service: Optional pre-configured client instance (used for testing/mocking).
        parent_note_ids: Optional list of parent note IDs to sync only specific subtrees.
        include_archived: Whether to ingest archived notes (defaults to False, archiving triggers tombstone).
        max_requests: Request limit per run.

    Returns:
        A configured dlt resource with merge write_disposition and _deleted hard-delete column.
    """
    if not isinstance(resource_name, str) or not resource_name.strip():
        raise ValueError("slite_source requires a non-empty resource_name")
    if service is None and not api_key:
        raise ValueError("slite_source requires an api_key or a service client")

    client = service if service is not None else SliteClient(api_key or "")
    stats: dict[str, int] = {}

    if _has_dlt and hasattr(dlt, "resource"):
        @dlt.resource(
            name=resource_name,
            primary_key="id",
            write_disposition="merge",
            columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
        )
        def slite_workspace():
            stats.clear()
            state = dlt.current.resource_state() if hasattr(dlt, "current") else {}
            yield from _iter_notes(
                client=client,
                state=state,
                stats=stats,
                parent_note_ids=parent_note_ids,
                include_archived=include_archived,
                max_requests=max_requests,
            )

        resource = slite_workspace()
        setattr(resource, DOCUMENT_SOURCE_ATTR, "slite")
        if dlt_utils and hasattr(dlt_utils, "PIPELINE_SCOPE_ATTR"):
            setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
        resource.cognee_sync_stats = stats
        return resource

    # Fallback when running outside dlt runtime
    class _StandinResource:
        def __init__(self):
            self.name = resource_name
            self.cognee_sync_stats = stats
            setattr(self, DOCUMENT_SOURCE_ATTR, "slite")

        def __iter__(self):
            yield from _iter_notes(
                client=client,
                state={},
                stats=stats,
                parent_note_ids=parent_note_ids,
                include_archived=include_archived,
                max_requests=max_requests,
            )

    return _StandinResource()
