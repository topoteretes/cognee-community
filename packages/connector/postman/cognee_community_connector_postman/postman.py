"""DLT source for Postman collections with incremental sync and upstream deletion.

Sync Postman collections, folders, and request descriptions into Cognee knowledge graph.
Tags DLT source with DOCUMENT_SOURCE_ATTR = "postman" for Cognee document-mode ingestion.
Zero emdashes across all code, docstrings, and comments.
"""

from __future__ import annotations

import os
from typing import Any

from cognee_community_connector_postman.client import (
    PostmanClient,
    PostmanNotFoundError,
)
from cognee_community_connector_postman.renderer import render_collection_documents

try:
    from cognee.shared.logging_utils import get_logger

    logger = get_logger("postman_connector")
except ImportError:
    import logging

    logger = logging.getLogger("postman_connector")

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

POSTMAN_SOURCE_NAME: str = "postman"
POSTMAN_TABLE_NAME: str = "postman_documents"
POSTMAN_RESOURCE_NAME: str = "postman_documents"

_EXTRA_HINT: str = 'The Postman connector requires dlt: pip install "dlt[sqlalchemy]>=1.9.0,<2"'

try:
    import dlt
    from dlt.sources import DltSource

    _HAS_DLT: bool = True
except ImportError:
    dlt = None  # type: ignore[assignment]
    DltSource = Any  # type: ignore[misc,assignment]
    _HAS_DLT = False

_LOCAL_STATE: dict[str, Any] = {}


def _timestamps_equal(ts1: str | None, ts2: str | None) -> bool:
    """Compare two ISO 8601 timestamps for temporal equality.

    Returns False if either timestamp is None or empty.
    Fast path checks string equality. Fallback parses ISO 8601 with timezone.
    """
    if not ts1 or not ts2:
        return False
    if ts1 == ts2:
        return True
    try:
        from datetime import UTC, datetime

        norm1 = str(ts1).strip().replace("Z", "+00:00")
        norm2 = str(ts2).strip().replace("Z", "+00:00")
        dt1 = datetime.fromisoformat(norm1)
        dt2 = datetime.fromisoformat(norm2)
        if dt1.tzinfo is None and dt2.tzinfo is not None:
            dt1 = dt1.replace(tzinfo=UTC)
        elif dt2.tzinfo is None and dt1.tzinfo is not None:
            dt2 = dt2.replace(tzinfo=UTC)
        return dt1 == dt2
    except (ValueError, TypeError, AttributeError):
        return False


def _is_gone(exc: Exception) -> bool:
    """Check if exception indicates a resource is permanently deleted or missing (404/410)."""
    if isinstance(exc, PostmanNotFoundError):
        return True
    if isinstance(exc, KeyError) and not isinstance(exc, IndexError | TypeError):
        return True
    status = (
        getattr(exc, "status_code", None)
        or getattr(exc, "status", None)
        or getattr(getattr(exc, "response", None), "status_code", None)
        or getattr(exc, "code", None)
    )
    return status in (404, 410)


class _DltResourceShim:
    """Lightweight fallback stand-in for DltResource when dlt is unavailable."""

    def __init__(
        self,
        func: Any,
        name: str = POSTMAN_RESOURCE_NAME,
        primary_key: str = "id",
        write_disposition: str = "replace",
    ) -> None:
        self.func = func
        self.name = name
        self.primary_key = primary_key
        self.write_disposition = write_disposition
        self.state: dict[str, Any] = {}

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.func(*args, resource_state=self.state, **kwargs)

    def __iter__(self) -> Any:
        return iter(self.func(resource_state=self.state))


class _DltSourceShim:
    """Lightweight fallback stand-in for DltSource when dlt is unavailable."""

    def __init__(
        self,
        resource: _DltResourceShim,
        name: str = POSTMAN_SOURCE_NAME,
    ) -> None:
        self.name = name
        self.resources: dict[str, _DltResourceShim] = {resource.name: resource}
        setattr(self, resource.name, resource)

    def __iter__(self) -> Any:
        for res in self.resources.values():
            yield from res

    def __getitem__(self, key: str) -> _DltResourceShim:
        return self.resources[key]


def postman_source(
    api_key: str | None = None,
    collection_ids: list[str] | None = None,
    workspace_id: str | None = None,
    client: Any | None = None,
) -> Any:
    """Create a DLT source yielding Postman collection markdown documents.

    Full snapshot sync with replace write disposition and incremental fetch
    optimization driven by collection updatedAt timestamps.

    Args:
        api_key: Postman API key. Falls back to POSTMAN_API_KEY environment variable.
        collection_ids: Optional list of collection IDs or UIDs to restrict ingestion to.
        workspace_id: Optional Postman workspace ID filter.
        client: Pre-built PostmanClient (or test double). When omitted, a client
            is instantiated using api_key.

    Returns:
        A DLT source yielding PostmanDocumentRow dictionaries tagged for Cognee.

    Raises:
        ValueError: If api_key is missing and no client was provided.
    """
    if client is None:
        resolved_key = api_key or os.environ.get("POSTMAN_API_KEY")
        if not resolved_key or not str(resolved_key).strip():
            raise ValueError("Postman API key required: pass api_key= or set POSTMAN_API_KEY.")
        resolved_client = PostmanClient(api_key=str(resolved_key).strip())
    else:
        resolved_client = client

    def _sync_generator(resource_state: dict[str, Any] | None = None) -> Any:
        state: dict[str, Any]
        if resource_state is not None:
            state = resource_state
        elif _HAS_DLT and dlt is not None and hasattr(dlt, "current"):
            try:
                state = dlt.current.resource_state()
            except Exception:
                state = _LOCAL_STATE
        else:
            state = _LOCAL_STATE

        collections_cache: dict[str, Any] = state.setdefault("collections", {})

        # 1. Fetch available collections from Postman API
        try:
            summaries = resolved_client.get_collections(workspace_id=workspace_id)
        except Exception as exc:
            logger.error("Postman: failed to list collections: %s", exc)
            raise

        if not isinstance(summaries, list):
            summaries = []

        # 2. Apply collection filtering with dual id and uid matching
        clean_collection_ids = (
            list(
                dict.fromkeys(
                    str(cid).strip() for cid in collection_ids if cid and str(cid).strip()
                )
            )
            if collection_ids is not None
            else None
        )
        if clean_collection_ids is not None:
            filter_set = set(clean_collection_ids)
            target_collections: list[dict[str, Any]] = [
                c
                for c in summaries
                if isinstance(c, dict)
                and (
                    str(c.get("uid") or "").strip() in filter_set
                    or str(c.get("id") or "").strip() in filter_set
                )
            ]
            matched_filter_values = {
                str(c.get("uid") or "").strip()
                for c in target_collections
                if isinstance(c, dict) and c.get("uid")
            } | {
                str(c.get("id") or "").strip()
                for c in target_collections
                if isinstance(c, dict) and c.get("id")
            }
            unmatched_cids = [
                cid for cid in clean_collection_ids if cid not in matched_filter_values
            ]
            for cid in unmatched_cids:
                try:
                    detail = resolved_client.get_collection(cid)
                    info = detail.get("info", {}) if isinstance(detail, dict) else {}
                    effective_id = info.get("_postman_id") or info.get("id") or cid
                    target_collections.append(
                        {
                            "id": str(effective_id).strip(),
                            "uid": cid,
                            "name": info.get("name", cid),
                            "updatedAt": (info.get("updatedAt") or info.get("updated_at")),
                            "_prefetched_detail": detail,
                        }
                    )
                except Exception as exc:
                    if _is_gone(exc):
                        logger.warning(
                            "Postman: collection '%s' not found during direct lookup, skipping: %s",
                            cid,
                            exc,
                        )
                        continue
                    raise
        else:
            filter_set = set()
            target_collections = [c for c in summaries if isinstance(c, dict)]

        # 3. Detect upstream deletions and evict stale collections from cache
        live_uids: set[str] = set()
        for c in target_collections:
            if isinstance(c, dict):
                uid = str(c.get("uid") or c.get("id") or "").strip()
                if uid:
                    live_uids.add(uid)

        for cached_uid in list(collections_cache.keys()):
            if collection_ids is not None and cached_uid not in filter_set:
                continue
            if cached_uid not in live_uids:
                del collections_cache[cached_uid]
                logger.info(
                    "Postman: evicted removed collection '%s' from state cache.",
                    cached_uid,
                )

        # 4. Iterate over active collections and yield document rows
        total_yielded = 0
        cache_hits = 0
        fresh_fetches = 0

        for col_meta in target_collections:
            col_uid = str(col_meta.get("uid") or col_meta.get("id") or "").strip()
            if not col_uid:
                continue

            current_updated_at = (
                str(col_meta.get("updatedAt")).strip()
                if col_meta.get("updatedAt")
                else (
                    str(col_meta.get("updated_at")).strip() if col_meta.get("updated_at") else None
                )
            )

            cached_entry = collections_cache.get(col_uid)
            is_cache_hit = False
            cached_docs: list[dict[str, Any]] = []

            if cached_entry and isinstance(cached_entry, dict) and current_updated_at:
                cached_ts = cached_entry.get("updatedAt") or cached_entry.get("updated_at")
                docs = (
                    cached_entry.get("docs")
                    if "docs" in cached_entry
                    else cached_entry.get("documents")
                )
                if (
                    isinstance(docs, list)
                    and len(docs) > 0
                    and _timestamps_equal(
                        current_updated_at,
                        str(cached_ts).strip() if cached_ts else None,
                    )
                ):
                    is_cache_hit = True
                    cached_docs = docs

            if is_cache_hit:
                cache_hits += 1
                logger.debug(
                    "Postman: collection '%s' unchanged at %s; re-yielding %d cached rows.",
                    col_uid,
                    current_updated_at,
                    len(cached_docs),
                )
                for doc in cached_docs:
                    total_yielded += 1
                    yield doc
                continue

            # Cache miss or modified: fetch details, render, update cache, and yield
            fresh_fetches += 1
            logger.info("Postman: fetching details for collection uid='%s'.", col_uid)
            try:
                if "_prefetched_detail" in col_meta and col_meta["_prefetched_detail"] is not None:
                    col_json = col_meta["_prefetched_detail"]
                else:
                    col_json = resolved_client.get_collection(col_uid)
            except Exception as exc:
                if _is_gone(exc):
                    logger.warning(
                        "Postman: collection '%s' is gone (%s), skipping.",
                        col_uid,
                        exc,
                    )
                    collections_cache.pop(col_uid, None)
                    continue
                raise

            rendered_docs = render_collection_documents(col_json, collection_uid=col_uid)

            detail_info = col_json.get("info", {}) if isinstance(col_json, dict) else {}
            detail_ts = None
            if isinstance(detail_info, dict):
                detail_ts = detail_info.get("updatedAt") or detail_info.get("updated_at")

            effective_updated_at = str(current_updated_at or detail_ts or "").strip()

            # Atomic dual-key cache update
            collections_cache[col_uid] = {
                "updatedAt": effective_updated_at,
                "updated_at": effective_updated_at,
                "docs": rendered_docs,
                "documents": rendered_docs,
            }

            logger.info(
                "Postman: synced %d document(s) for collection uid='%s' (updatedAt=%s).",
                len(rendered_docs),
                col_uid,
                effective_updated_at,
            )
            for doc in rendered_docs:
                total_yielded += 1
                yield doc

        logger.info(
            "Postman sync finished: %d total document(s) yielded "
            "(%d cache hits, %d fresh fetches).",
            total_yielded,
            cache_hits,
            fresh_fetches,
        )

    # 5. Build DLT resource and source
    if _HAS_DLT and dlt is not None:

        @dlt.resource(
            name=POSTMAN_TABLE_NAME,
            primary_key="id",
            write_disposition="replace",
        )
        def postman_documents() -> Any:
            yield from _sync_generator()

        @dlt.source(name=POSTMAN_SOURCE_NAME)
        def _postman() -> Any:
            return postman_documents

        source = _postman()
    else:
        logger.warning(
            "dlt package is not installed. Using fallback source shim. Hint: %s",
            _EXTRA_HINT,
        )
        resource_shim = _DltResourceShim(
            _sync_generator,
            name=POSTMAN_TABLE_NAME,
            primary_key="id",
            write_disposition="replace",
        )
        source = _DltSourceShim(resource_shim, name=POSTMAN_SOURCE_NAME)

    # 6. Opt into Cognee document-mode ingestion pipeline
    setattr(source, DOCUMENT_SOURCE_ATTR, POSTMAN_SOURCE_NAME)
    return source
