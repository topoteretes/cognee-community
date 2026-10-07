"""dlt source for Sanity CMS documents (incremental sync + forget-on-delete).

Fetches Sanity documents via GROQ from the Sanity HTTP API, then yields them
as a dlt resource for cognee's ingestion pipeline.

Sanity documents are ingested as *normal documents*: the source declares
``cognee_document_source = "sanity"``, so ``resolve_dlt_sources`` tags each row
``external_metadata["source"] = "sanity"`` (not ``"dlt"``). Documents therefore
flow through the standard cognify entity-extraction pipeline — the right
treatment for CMS prose content.

Incremental sync uses the ``_updatedAt`` field in GROQ filters with a small
overlap window to catch late-updating documents.

Deletion: ``write_disposition="replace"`` so each sync produces the complete
current set of documents matching the configured query/type; anything dropped
from the listing gets cleaned up via orphan cleanup.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("sanity_connector")

SANITY_TABLE_NAME = "sanity_documents"
SANITY_SOURCE_NAME = "sanity"

_MAX_RETRIES = 5
_OVERLAP_WINDOW_MINUTES = 5

_EXTRA_HINT = (
    'The Sanity connector requires the "sanity" extra: pip install "cognee[sanity]" '
    "(provides dlt and requests)."
)


def sanity_source(
    project_id: str | None = None,
    api_token: str | None = None,
    dataset: str = "production",
    api_version: str = "2021-10-21",
    document_types: list[str] | None = None,
    groq_filter: str | None = None,
    client: Any = None,
):
    """Create a dlt source that yields Sanity CMS documents.

    Args:
        project_id: Sanity project ID. Falls back to ``SANITY_PROJECT_ID``.
        api_token: Sanity API token (read-only). Falls back to ``SANITY_API_TOKEN``.
        dataset: Sanity dataset name, default ``"production"``.
        api_version: Sanity API version date string (e.g. ``"2021-10-21"``).
        document_types: Optional list of Sanity ``_type`` values to restrict
            ingestion. When omitted, all document types are fetched.
        groq_filter: Optional additional GROQ filter expression appended to the
            query (e.g. ``"status == 'published'"``).
        client: Pre-built HTTP client callable (test-injection point).

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    if client is None:
        try:
            import requests
        except ImportError as exc:
            raise ImportError(_EXTRA_HINT) from exc

        resolved_project = project_id or os.environ.get("SANITY_PROJECT_ID")
        resolved_token = api_token or os.environ.get("SANITY_API_TOKEN")
        if not resolved_project or not resolved_token:
            raise ValueError(
                "Sanity project_id and api_token required: pass them explicitly or "
                "set SANITY_PROJECT_ID + SANITY_API_TOKEN environment variables."
            )

        session = requests.Session()
        session.headers.update({"Authorization": f"Bearer {resolved_token}"})
        base_url = f"https://{resolved_project}.api.sanity.io/v{api_version}"

        def _api_request(method: str, path: str, **kwargs: Any) -> Any:
            url = f"{base_url}{path}"
            for attempt in range(_MAX_RETRIES):
                response = session.request(method, url, **kwargs)
                if response.status_code == 429:
                    retry_after = int(response.headers.get("retry-after", 2**attempt))
                    time.sleep(retry_after)
                    continue
                if response.status_code >= 500 and attempt < _MAX_RETRIES - 1:
                    time.sleep(2**attempt)
                    continue
                response.raise_for_status()
                return response.json()
            raise RuntimeError(f"Sanity API: {method} {path} failed after {_MAX_RETRIES} retries.")

        client = _api_request

    @dlt.resource(
        name=SANITY_TABLE_NAME,
        primary_key="_id",
        write_disposition="replace",
    )
    def sanity_documents() -> Iterator[dict[str, Any]]:
        import dlt as _runtime_dlt

        state = _runtime_dlt.current.resource_state()
        last_updated = state.get("last_updated_at")
        if last_updated:
            cursor_dt = datetime.fromisoformat(last_updated) - timedelta(
                minutes=_OVERLAP_WINDOW_MINUTES
            )
            cursor_iso = cursor_dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            cursor_iso = None

        count = 0
        for doc in _iter_documents(client, dataset, document_types, groq_filter, cursor_iso):
            count += 1
            yield _doc_to_row(doc)
            updated_at = doc.get("_updatedAt")
            if updated_at:
                current = state.get("last_updated_at")
                if current is None or updated_at > current:
                    state["last_updated_at"] = updated_at

        logger.info("Sanity: synced %d document(s).", count)

    @dlt.source(name=SANITY_SOURCE_NAME)
    def _sanity() -> Any:
        return sanity_documents

    source = _sanity()
    setattr(source, DOCUMENT_SOURCE_ATTR, SANITY_SOURCE_NAME)
    return source


def _iter_documents(
    client: Any,
    dataset: str,
    document_types: list[str] | None,
    groq_filter: str | None,
    cursor_iso: str | None,
) -> Iterable[dict[str, Any]]:
    """Yield Sanity documents matching the configured filters via GROQ pagination."""
    # Build GROQ filter
    filters: list[str] = []
    if document_types:
        types_or = " || ".join(f'_type == "{t}"' for t in document_types)
        filters.append(f"({types_or})")
    if cursor_iso:
        filters.append(f'_updatedAt > "{cursor_iso}"')
    if groq_filter:
        filters.append(f"({groq_filter})")

    where = " && ".join(filters) if filters else ""
    groq = f"*[{where}]" if where else "*"

    # Paginate with Sanity's standard cursor-based approach
    limit = 100
    offset = 0
    while True:
        query = f"{groq} | order(_updatedAt asc) [{offset}...{offset + limit}]"
        data = client(
            "GET",
            f"/data/query/{dataset}",
            params={"query": query, "perspective": "published"},
        )
        docs = data.get("result", [])
        if not docs:
            break
        yield from docs
        if len(docs) < limit:
            break
        offset += limit


def _doc_to_row(doc: dict[str, Any]) -> dict[str, Any]:
    """Transform a raw Sanity document into a cognee document row."""
    doc_id = doc.get("_id", "")
    doc_type = doc.get("_type", "unknown")

    # Build a prose-friendly text body from common Sanity fields.
    # Custom schemas will vary; this captures standard patterns.
    body_parts: list[str] = []
    body_parts.append(f"Sanity document ({doc_type}): {doc_id}")

    for field in ["title", "name", "heading", "headline"]:
        if doc.get(field):
            body_parts.append(f"Title: {doc[field]}")
            break

    for field in ["slug", "url", "path"]:
        if doc.get(field):
            slug_val = doc[field]
            if isinstance(slug_val, dict) and "current" in slug_val:
                body_parts.append(f"Slug: {slug_val['current']}")
            else:
                body_parts.append(f"{field}: {slug_val}")

    for field in ["body", "content", "description", "excerpt", "text"]:
        if doc.get(field):
            val = doc[field]
            text = _extract_portable_text(val)
            if text:
                body_parts.append(text)

    body_parts.append(f"Created: {doc.get('_createdAt', 'unknown')}")
    body_parts.append(f"Updated: {doc.get('_updatedAt', 'unknown')}")
    body_parts.append(f"Revision: {doc.get('_rev', 'unknown')}")

    return {
        "_id": doc_id,
        "_type": doc_type,
        "_created_at": doc.get("_createdAt"),
        "_updated_at": doc.get("_updatedAt"),
        "_rev": doc.get("_rev"),
        "title": _first_string(doc, ["title", "name", "heading", "headline"]),
        "text": "\n\n".join(body_parts),
        "raw": doc,
        "source": SANITY_SOURCE_NAME,
    }


def _extract_portable_text(value: Any) -> str:
    """Extract plain text from Sanity Portable Text / strings / rich text."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for block in value:
            if isinstance(block, dict) and block.get("_type") == "block":
                for child in block.get("children", []):
                    if isinstance(child, dict) and child.get("_type") == "span":
                        parts.append(child.get("text", ""))
                parts.append("\n")
        return "".join(parts).strip()
    if isinstance(value, dict):
        # Try common rich-text / string fields
        for key in ["text", "content", "body", "description"]:
            if key in value:
                result = _extract_portable_text(value[key])
                if result:
                    return result
    return ""


def _first_string(doc: dict[str, Any], fields: list[str]) -> str | None:
    """Return the first non-empty string value from the listed field names."""
    for field in fields:
        val = doc.get(field)
        if isinstance(val, str) and val.strip():
            return val
    return None
