"""Nuclino connector for cognee (HTTP foundation & client utilities).

Handles authentication, transient error retry / rate-limit backoff, and cursor
pagination over the Nuclino REST v0 API (https://api.nuclino.com/v0).
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("nuclino_connector")

# Nuclino REST API v0 base URL.
NUCLINO_API_BASE = "https://api.nuclino.com/v0"

# Retry budget for rate-limited (429) or transient 5xx responses.
_MAX_RETRIES = 5

# Default limit for collection listings.
_DEFAULT_LIMIT = 100

_EXTRA_HINT = (
    'The Nuclino connector requires "requests". Install with:\n'
    "    pip install requests\n"
    "or install the connector package dependencies:\n"
    "    cd packages/connector/nuclino && uv sync"
)


def _make_session(api_key: str | None = None) -> Any:
    """Build a ``requests.Session`` authenticated with a Nuclino API key.

    Nuclino REST v0 expects raw key authentication without a 'Bearer' prefix:
    ``Authorization: <API_KEY>``.
    """
    try:
        import requests
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    resolved_key = api_key or os.environ.get("NUCLINO_API_KEY")
    if not resolved_key:
        raise ValueError("Nuclino API key required: pass api_key= or set NUCLINO_API_KEY.")

    session = requests.Session()
    session.headers.update(
        {
            "Authorization": resolved_key,
            "Accept": "application/json",
        }
    )
    return session


def _is_transient(exc: Exception) -> bool:
    """Classify whether an exception represents a transient/rate-limit error worth retrying."""
    try:
        import requests

        request_exceptions = (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        )
    except ImportError:
        request_exceptions = ()

    if isinstance(exc, request_exceptions):
        return True

    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    if status_code is None:
        status_code = getattr(exc, "status_code", None)
    if status_code is None:
        status_code = getattr(exc, "status", None)

    if status_code is not None:
        return status_code in (429, 500, 502, 503, 504)

    return False


def _retry_after(headers: Any, attempt: int) -> float:
    """Return seconds to wait before retrying: Retry-After header, else exponential backoff."""
    if headers:
        header = None
        if hasattr(headers, "get"):
            header = headers.get("retry-after") or headers.get("Retry-After")
        if header is not None:
            try:
                val = float(header)
                if val >= 0:
                    return val
            except (TypeError, ValueError):
                pass
    return float(2**attempt)


def _request(method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Execute an HTTP request with retry/backoff on rate limits and transient errors.

    Retries up to ``_MAX_RETRIES`` on transient errors (429, 5xx, timeouts).
    Permanent errors (401, 403, 404) are raised immediately.
    """
    for attempt in range(_MAX_RETRIES):
        try:
            response = method(*args, **kwargs)
            if hasattr(response, "raise_for_status"):
                response.raise_for_status()
            return response
        except Exception as exc:
            if attempt == _MAX_RETRIES - 1 or not _is_transient(exc):
                raise
            headers = getattr(getattr(exc, "response", None), "headers", None)
            if headers is None:
                headers = getattr(exc, "headers", None)
            delay = _retry_after(headers, attempt)
            logger.warning(
                "Nuclino: %s — retrying in %.1fs (%d/%d).",
                exc,
                delay,
                attempt + 1,
                _MAX_RETRIES,
            )
            time.sleep(delay)


def _paginate(
    session: Any,
    url: str,
    params: dict[str, Any] | None = None,
    limit: int = _DEFAULT_LIMIT,
) -> Iterator[dict[str, Any]]:
    """Yield items across Nuclino cursor-paginated collection endpoints.

    Nuclino list endpoints wrap items under ``response.json()["data"]["results"]``
    and paginate via the ``after=<last_id>`` query parameter.

    Fails closed: raises ValueError or RuntimeError on unexpected response structures
    or cursor stagnation to prevent partial sweeps from causing accidental mass deletion.
    """
    next_params = dict(params or {})
    next_params.setdefault("limit", limit)
    effective_limit = next_params.get("limit", limit)
    seen_cursors: set[str] = set()

    while True:
        response = _request(session.get, url, params=next_params)
        payload = response.json() if hasattr(response, "json") else response

        if not isinstance(payload, dict):
            raise ValueError(
                f"Nuclino API response must be a JSON object, got {type(payload).__name__}."
            )

        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError(f"Nuclino API response missing 'data' object in payload: {payload!r}.")

        results = data.get("results")
        if not isinstance(results, list):
            raise ValueError(f"Nuclino API response missing 'results' list in 'data': {data!r}.")

        # Valid termination: empty results on initial or subsequent page
        if not results:
            return

        for item in results:
            if not isinstance(item, dict):
                raise ValueError(
                    f"Nuclino result item must be a dictionary, got {type(item).__name__}."
                )
            yield item

        # Valid termination: final page returned fewer than limit records
        if len(results) < effective_limit:
            return

        last_item = results[-1]
        last_id = last_item.get("id")
        if not last_id or not isinstance(last_id, str):
            raise ValueError(
                f"Nuclino final item in full page missing valid string 'id': {last_item!r}."
            )

        if last_id in seen_cursors or next_params.get("after") == last_id:
            raise RuntimeError(
                f"Nuclino pagination cursor stagnated on id {last_id!r}; "
                "aborting to prevent partial sweep or infinite loop."
            )

        seen_cursors.add(last_id)
        next_params["after"] = last_id


def _resolve_workspace_ids(
    session: Any,
    workspace_ids: list[str] | None = None,
    team_id: str | None = None,
) -> list[str]:
    """Return a deterministic list of workspace IDs to sync.

    If ``workspace_ids`` is provided and non-empty:
      - IDs are normalized to strings
      - Deduplicated while preserving insertion order
      - Returned without calling the Nuclino API.
    Otherwise, calls ``GET /v0/workspaces`` using ``_paginate``.
    If ``team_id`` is supplied, passes ``teamId=<team_id>``.
    Fails closed if an object does not contain a valid string ``id``.
    """
    if workspace_ids is not None and len(workspace_ids) > 0:
        deduped: list[str] = []
        seen: set[str] = set()
        for wid in workspace_ids:
            normalized = str(wid).strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                deduped.append(normalized)
        return deduped

    params: dict[str, Any] = {}
    if team_id:
        params["teamId"] = str(team_id).strip()

    discovered_ids: list[str] = []
    for ws in _paginate(session, f"{NUCLINO_API_BASE}/workspaces", params=params):
        if not isinstance(ws, dict):
            raise ValueError(f"Nuclino workspace must be a dictionary, got {type(ws).__name__}.")
        ws_id = ws.get("id")
        if not ws_id or not isinstance(ws_id, str) or not ws_id.strip():
            raise ValueError(f"Nuclino workspace missing valid string 'id': {ws!r}.")
        discovered_ids.append(ws_id.strip())

    return discovered_ids


def _iter_item_metadata(
    session: Any,
    workspace_ids: list[str],
) -> Iterator[dict[str, Any]]:
    """Yield item and collection metadata across the specified workspaces.

    Calls ``GET /v0/items?workspaceId=<workspace_id>`` using ``_paginate``.
    Yields both ``object == 'item'`` and ``object == 'collection'``.
    Validates that every returned object has:
      - valid string ``id``
      - valid string ``workspaceId``
      - ``object`` equal to either ``'item'`` or ``'collection'``
    Fails closed on unexpected types or schema. Does not fetch item content.
    """
    for ws_id in workspace_ids:
        params = {"workspaceId": str(ws_id).strip()}
        for obj in _paginate(session, f"{NUCLINO_API_BASE}/items", params=params):
            if not isinstance(obj, dict):
                raise ValueError(
                    f"Nuclino metadata object must be a dictionary, got {type(obj).__name__}."
                )

            obj_type = obj.get("object")
            if obj_type not in ("item", "collection"):
                raise ValueError(
                    f"Nuclino object has unexpected type {obj_type!r} "
                    f"(expected 'item' or 'collection'): {obj!r}."
                )

            obj_id = obj.get("id")
            if not obj_id or not isinstance(obj_id, str) or not obj_id.strip():
                raise ValueError(f"Nuclino {obj_type} metadata missing valid string 'id': {obj!r}.")

            ws_ref = obj.get("workspaceId")
            if not ws_ref or not isinstance(ws_ref, str) or not ws_ref.strip():
                raise ValueError(
                    f"Nuclino {obj_type} metadata missing valid string 'workspaceId': {obj!r}."
                )

            yield obj


def _fetch_item(
    session: Any,
    item_id: str,
) -> dict[str, Any] | None:
    """Fetch full item or collection details including Markdown content.

    Calls ``GET /v0/items/{item_id}``.
    Returns the full ``data`` object containing Markdown ``content``.
    HTTP 404 returns ``None`` (vanished or deleted between list and fetch).
    HTTP 401, 403 and other permanent 4xx errors propagate.
    Validates:
      - ``object`` is ``'item'`` or ``'collection'``
      - ``id`` is a string and matches ``item_id``
      - ``content`` is a string (fails if content is malformed)
    """
    url = f"{NUCLINO_API_BASE}/items/{item_id}"
    try:
        response = _request(session.get, url)
    except Exception as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if status_code is None:
            status_code = getattr(exc, "status_code", None)
        if status_code is None:
            status_code = getattr(exc, "status", None)

        if status_code == 404:
            return None
        raise

    payload = response.json() if hasattr(response, "json") else response
    if not isinstance(payload, dict):
        raise ValueError(
            f"Nuclino item response must be a JSON object, got {type(payload).__name__}."
        )

    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError(f"Nuclino item response missing 'data' object in payload: {payload!r}.")

    obj_type = data.get("object")
    if obj_type not in ("item", "collection"):
        raise ValueError(
            f"Nuclino item response has unexpected object type {obj_type!r} "
            f"(expected 'item' or 'collection'): {data!r}."
        )

    returned_id = data.get("id")
    if not returned_id or not isinstance(returned_id, str):
        raise ValueError(f"Nuclino item response missing valid string 'id': {data!r}.")

    if returned_id != item_id:
        raise ValueError(
            f"Nuclino item ID mismatch: requested {item_id!r}, received {returned_id!r}."
        )

    if "content" not in data or not isinstance(data["content"], str):
        raise ValueError(f"Nuclino item response missing valid string 'content': {data!r}.")

    return data


def _item_to_row(item: dict[str, Any]) -> dict[str, Any]:
    """Transform a Nuclino item or collection into a Cognee-compatible document row.

    Excludes volatile metadata (e.g. timestamps, versions, childIds, fields, user IDs)
    to maintain content-hash stability and prevent redundant cognify re-runs.
    """
    if not isinstance(item, dict):
        raise ValueError(f"Expected dictionary for item, got {type(item).__name__}.")

    item_id = item.get("id")
    if not item_id or not isinstance(item_id, str) or not item_id.strip():
        raise ValueError(f"Item missing valid string 'id': {item!r}.")

    workspace_id = item.get("workspaceId")
    if not workspace_id or not isinstance(workspace_id, str) or not workspace_id.strip():
        raise ValueError(f"Item missing valid string 'workspaceId': {item!r}.")

    return {
        "id": str(item_id).strip(),
        "title": item.get("title") or "",
        "content": item.get("content") or "",
        "url": item.get("url") or "",
        "workspace_id": str(workspace_id).strip(),
        "_deleted": False,
    }


def _deleted_row(item_id: str) -> dict[str, Any]:
    """Return a Cognee deletion tombstone for a removed item or collection."""
    if not item_id or not isinstance(item_id, str) or not item_id.strip():
        raise ValueError(f"Invalid item_id for deletion tombstone: {item_id!r}.")

    return {
        "id": item_id.strip(),
        "_deleted": True,
    }


def sync_items(
    session: Any,
    state: dict[str, Any],
    *,
    workspace_ids: list[str] | None = None,
    team_id: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Incrementally synchronize Nuclino items and collections into Cognee document rows.

    Maintains per-item synchronization versions in ``state["item_versions"]`` using
    Nuclino's per-object ``lastUpdatedAt`` timestamp.

    Deletion reconciliation:
      - Sweeps lightweight metadata across all targeted workspaces.
      - Fails closed on malformed metadata, missing ``lastUpdatedAt``, or duplicate conflicts.
      - Authoritative empty sweep: if a successful metadata sweep returns 0 objects,
        all previously known IDs are emitted as deletion tombstones and state becomes empty.
      - Changed/new items are fetched individually via ``_fetch_item``.
      - Vanished items (HTTP 404 on detail fetch) are omitted if brand-new, or emitted
        as deletion tombstones if previously known.
      - State is only replaced after the full sweep and detail fetch succeed.
    """
    if not isinstance(state, dict):
        raise ValueError(f"State must be a dictionary, got {type(state).__name__}.")

    previous_raw = state.get("item_versions")
    if previous_raw is None:
        previous_versions: dict[str, str] = {}
    elif isinstance(previous_raw, dict):
        previous_versions = dict(previous_raw)
    else:
        raise ValueError(
            f"Expected dict for 'item_versions' in state, got {type(previous_raw).__name__}."
        )

    # A. Resolve scope
    resolved_workspaces = _resolve_workspace_ids(
        session,
        workspace_ids=workspace_ids,
        team_id=team_id,
    )

    # B. Complete metadata sweep
    current_metadata: dict[str, dict[str, Any]] = {}
    for obj in _iter_item_metadata(session, resolved_workspaces):
        obj_id = obj["id"]
        last_updated = obj.get("lastUpdatedAt")
        if not last_updated or not isinstance(last_updated, str) or not last_updated.strip():
            raise ValueError(
                f"Nuclino object {obj_id!r} missing valid non-empty string "
                f"'lastUpdatedAt': {obj!r}."
            )

        last_updated_str = last_updated.strip()
        if obj_id in current_metadata:
            existing_meta = current_metadata[obj_id]
            if (
                existing_meta.get("lastUpdatedAt") != last_updated_str
                or existing_meta.get("workspaceId") != obj.get("workspaceId")
                or existing_meta.get("object") != obj.get("object")
            ):
                raise ValueError(
                    f"Conflicting duplicate metadata for object {obj_id!r}: "
                    f"{existing_meta!r} vs {obj!r}."
                )
        current_metadata[obj_id] = obj

    # C. Determine changed/new objects and fetch details
    effective_current_versions: dict[str, str] = {}
    rows_to_yield: list[dict[str, Any]] = []

    for item_id, meta in sorted(current_metadata.items()):
        current_version = meta["lastUpdatedAt"].strip()
        prev_version = previous_versions.get(item_id)

        if prev_version is None or prev_version != current_version:
            detail = _fetch_item(session, item_id)
            if detail is None:
                # 404 race condition: item vanished between listing and detail fetch.
                # Excluded from effective_current_versions so that if it was in previous_versions,
                # it will be emitted as a deletion tombstone in step G.
                continue
            effective_current_versions[item_id] = current_version
            rows_to_yield.append(_item_to_row(detail))
        else:
            effective_current_versions[item_id] = current_version

    # G. Deletion detection
    deleted_ids = sorted(previous_versions.keys() - effective_current_versions.keys())
    for del_id in deleted_ids:
        rows_to_yield.append(_deleted_row(del_id))

    # H. State safety: replace state only after all operations succeed
    state["item_versions"] = {
        k: effective_current_versions[k] for k in sorted(effective_current_versions.keys())
    }

    yield from rows_to_yield


def nuclino_source(
    *,
    api_key: str | None = None,
    workspace_ids: list[str] | None = None,
    team_id: str | None = None,
    session: Any = None,
) -> Any:
    """Create a DLT resource configured for incremental sync of Nuclino items.

    Returns a ``dlt.resource`` configured with:
      - ``name="nuclino_items"``
      - ``primary_key="id"``
      - ``write_disposition="merge"``
      - ``columns={"_deleted": {"data_type": "bool", "hard_delete": True}}``
      - Tagged with ``DOCUMENT_SOURCE_ATTR = "nuclino"`` for Cognee document ingestion.

    When calling ``cognee.remember(...)``, explicitly pass ``write_disposition="merge"``:
    ```python
    await cognee.remember(
        nuclino_source(...),
        dataset_name="nuclino",
        primary_key="id",
        write_disposition="merge",
    )
    ```
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Nuclino connector requires "dlt". Install with:\n'
            "    pip install dlt\n"
            "or install the connector package dependencies:\n"
            "    cd packages/connector/nuclino && uv sync"
        ) from exc

    @dlt.resource(
        name="nuclino_items",
        primary_key="id",
        write_disposition="merge",
        columns={
            "_deleted": {
                "data_type": "bool",
                "hard_delete": True,
            }
        },
    )
    def nuclino_items() -> Iterator[dict[str, Any]]:
        client = session or _make_session(api_key)
        state = dlt.current.resource_state()

        yield from sync_items(
            client,
            state,
            workspace_ids=workspace_ids,
            team_id=team_id,
        )

    resource = nuclino_items()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "nuclino")
    return resource
