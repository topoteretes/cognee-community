"""Raindrop.io connector for Cognee.

This module exposes a small dlt-style source that can be handed to
``cognee.remember(...)`` or ``cognee.add(...)``. It reads bookmark metadata
(title, excerpt, URL, tags, collection) and normalizes it into a row schema
compatible with the rest of Cognee's ingestion pipeline.
"""

from __future__ import annotations

import os
from typing import Any, Iterable

import requests

API_URL = "https://api.raindrop.io/rest/v1"


class RaindropAPIError(RuntimeError):
    """Raised when the Raindrop API rejects a request."""


def _get_token(token: str | None = None) -> str:
    resolved = token or os.getenv("RAINDROP_API_TOKEN")
    if not resolved:
        raise ValueError("Raindrop API token missing. Pass token=... or set RAINDROP_API_TOKEN.")
    return resolved


def _extract_tags(raw_tags: Any) -> str:
    if not raw_tags:
        return ""
    if isinstance(raw_tags, str):
        return raw_tags
    return ", ".join(str(tag.get("title") if isinstance(tag, dict) else tag) for tag in raw_tags)


def normalize_bookmark(item: dict[str, Any]) -> dict[str, Any]:
    """Normalize one Raindrop bookmark into a Cognee-friendly row."""
    collection = item.get("collection") or {}
    tags = item.get("tags") or []

    return {
        "id": item.get("_id"),
        "title": item.get("title") or "",
        "excerpt": item.get("excerpt") or item.get("note") or "",
        "url": item.get("link") or "",
        "tags": _extract_tags(tags),
        "collection": (collection.get("title") if isinstance(collection, dict) else str(collection)) or "",
        "created_at": item.get("created"),
        "updated_at": item.get("updated"),
        "cover": item.get("cover") or "",
        "domain": item.get("domain") or "",
        "type": item.get("type") or "",
    }


def _fetch_page(
    token: str,
    *,
    collection_id: int | str | None = None,
    search: str | None = None,
    page: int = 0,
    page_size: int = 50,
) -> list[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}"}
    params: dict[str, Any] = {"page": page, "perpage": page_size}
    if collection_id is not None:
        params["collectionId"] = collection_id
    if search:
        params["search"] = search

    response = requests.get(f"{API_URL}/raindrops", headers=headers, params=params, timeout=30)
    if response.status_code >= 400:
        payload = response.json() if response.content else {}
        error_text = payload.get("error", {}).get("message") if isinstance(payload, dict) else str(payload)
        raise RaindropAPIError(f"Raindrop API request failed ({response.status_code}): {error_text}")

    payload = response.json()
    items = payload.get("items", []) if isinstance(payload, dict) else []
    return [item for item in items if isinstance(item, dict)]


def get_bookmarks(
    token: str | None = None,
    *,
    collection_id: int | str | None = None,
    search: str | None = None,
    page_size: int = 50,
) -> list[dict[str, Any]]:
    """Fetch all bookmarks for the current account (or a filtered subset)."""
    resolved_token = _get_token(token)
    rows: list[dict[str, Any]] = []
    page = 0

    while True:
        items = _fetch_page(
            resolved_token,
            collection_id=collection_id,
            search=search,
            page=page,
            page_size=page_size,
        )
        if not items:
            break
        rows.extend(normalize_bookmark(item) for item in items)
        if len(items) < page_size:
            break
        page += 1

    return rows


def raindrop_source(
    token: str | None = None,
    *,
    collection_id: int | str | None = None,
    search: str | None = None,
    page_size: int = 50,
):
    """Return a dlt-style source object for Raindrop bookmarks.

    This is intentionally lightweight and mirrors the project connector pattern,
    while keeping the logic easy to test without a live Raindrop account.
    """
    resolved_token = _get_token(token)

    try:
        import dlt
    except ImportError as exc:  # pragma: no cover - dependency handled at package level
        raise ImportError(
            'The Raindrop connector requires the "dlt" extra: pip install "cognee[raindrop]"'
        ) from exc

    @dlt.resource(name="raindrop_bookmarks", primary_key="id", write_disposition="replace")
    def raindrop_bookmarks():
        yield from get_bookmarks(
            resolved_token,
            collection_id=collection_id,
            search=search,
            page_size=page_size,
        )

    @dlt.source(name="raindrop")
    def _raindrop():
        return raindrop_bookmarks

    return _raindrop()


__all__ = ["RaindropAPIError", "get_bookmarks", "normalize_bookmark", "raindrop_source"]
