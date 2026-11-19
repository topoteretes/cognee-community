"""Hoppscotch API collections & environments connector for cognee.

Parses Hoppscotch collection exports (v1/v2 schema, folders, requests, parameters,
headers, auth configs, and request bodies) and environment variable exports into
rich, structured documents ready for :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_hoppscotch import hoppscotch_source

    await cognee.remember(
        hoppscotch_source(
            collection_path="/path/to/hoppscotch-collection.json",
            environment_path="/path/to/hoppscotch-environment.json",  # optional
        ),
        dataset_name="api_specs",
        max_rows_per_table=0,  # ingest the full collection snapshot
    )

Design
------
* **Primary key** — ``id = "hoppscotch:{collection_slug}:{folder_path}:{request_name}"``.
  Stable across export snapshots so incremental re-syncs detect updates.
* **Snapshot sync (``replace``)** — Hoppscotch exports are full snapshots of your API
  workspace. Using ``write_disposition="replace"`` ensures that any endpoint deleted
  from the collection upstream is automatically purged from the graph and vector index
  via cognee's orphan cleanup.
* **Agent-Optimized Content** — Endpoints are formatted into structured text detailing
  HTTP method, full URL path, active headers, query parameters, authentication, and
  sample request bodies so AI coding agents can accurately call endpoints without hallucinations.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

try:
    from cognee.shared.logging_utils import get_logger
    logger = get_logger("hoppscotch_connector")
except ImportError:
    import logging
    logger = logging.getLogger("hoppscotch_connector")

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

HOPPSCOTCH_TABLE_NAME = "hoppscotch_requests"
HOPPSCOTCH_SOURCE_NAME = "hoppscotch"


def _slugify(text: str) -> str:
    """Sanitize string for stable primary key construction."""
    cleaned = re.sub(r"[^a-zA-Z0-9_\-\.]+", "_", text.strip().lower())
    return cleaned.strip("_") or "default"


def _load_json_data(path: Path) -> Any:
    """Load JSON from a file, returning parsed structure or None on error."""
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Hoppscotch connector: failed to parse JSON from %s: %s", path, exc)
        return None


def load_environment_variables(env_source: str | os.PathLike | dict | list | None) -> dict[str, str]:
    """Load and normalize Hoppscotch environment variables.

    Supports:
    - Path to an environment export JSON
    - Dictionary or list representing Hoppscotch environment format:
      ``{"name": "...", "variables": [{"key": "baseUrl", "value": "https://api.com"}]}``
    """
    if env_source is None:
        return {}

    raw_data: Any = None
    if isinstance(env_source, (str, os.PathLike)):
        p = Path(env_source)
        if not p.is_file():
            logger.warning("Hoppscotch connector: environment file not found: %s", p)
            return {}
        raw_data = _load_json_data(p)
    elif isinstance(env_source, (dict, list)):
        raw_data = env_source

    variables: dict[str, str] = {}
    if isinstance(raw_data, list):
        for item in raw_data:
            if isinstance(item, dict):
                # Could be a list of env objects or direct list of variable dicts
                if "variables" in item and isinstance(item["variables"], list):
                    for var in item["variables"]:
                        if isinstance(var, dict) and "key" in var:
                            variables[str(var["key"])] = str(var.get("value", ""))
                elif "key" in item:
                    variables[str(item["key"])] = str(item.get("value", ""))
    elif isinstance(raw_data, dict):
        var_list = raw_data.get("variables")
        if isinstance(var_list, list):
            for var in var_list:
                if isinstance(var, dict) and "key" in var:
                    variables[str(var["key"])] = str(var.get("value", ""))
        else:
            # Flat key-value dict fallback
            for k, v in raw_data.items():
                if isinstance(v, (str, int, float, bool)):
                    variables[str(k)] = str(v)

    return variables


def _substitute_variables(text: str, variables: dict[str, str]) -> str:
    """Substitute Hoppscotch <<variable>> or << variable >> placeholders."""
    if not variables or not text:
        return text

    def _replace(match: re.Match) -> str:
        var_name = match.group(1).strip()
        return variables.get(var_name, match.group(0))

    return re.sub(r"<<\s*([a-zA-Z0-9_\-\.]+)\s*>>", _replace, text)


def _format_request_document(
    request: dict[str, Any],
    collection_name: str,
    folder_path: str,
    variables: dict[str, str],
) -> dict[str, Any]:
    """Format a Hoppscotch request into a rich, structured document for AI memory."""
    req_name = request.get("name") or "Unnamed Request"
    method = (request.get("method") or "GET").upper()
    raw_endpoint = request.get("endpoint") or ""
    endpoint = _substitute_variables(raw_endpoint, variables)

    # Breadcrumb path: e.g., "Payments > Subscriptions > Create Session"
    folder_breadcrumb = f"{collection_name} > {folder_path}" if folder_path else collection_name

    # Extract Active Headers
    headers_list: list[str] = []
    raw_headers = request.get("headers") or []
    if isinstance(raw_headers, list):
        for h in raw_headers:
            if isinstance(h, dict) and h.get("active", True) and h.get("key"):
                val = _substitute_variables(str(h.get("value", "")), variables)
                desc = f" ({h['description']})" if h.get("description") else ""
                headers_list.append(f"  - {h['key']}: {val}{desc}")

    # Extract Active Query Parameters
    params_list: list[str] = []
    raw_params = request.get("params") or []
    if isinstance(raw_params, list):
        for p in raw_params:
            if isinstance(p, dict) and p.get("active", True) and p.get("key"):
                val = _substitute_variables(str(p.get("value", "")), variables)
                desc = f" ({p['description']})" if p.get("description") else ""
                params_list.append(f"  - {p['key']}: {val}{desc}")

    # Extract Auth Configuration
    auth = request.get("auth") or {}
    auth_type = "None"
    auth_active = True
    if isinstance(auth, dict):
        auth_type = auth.get("authType") or "None"
        auth_active = auth.get("authActive", True)

    auth_display = auth_type if auth_active else f"{auth_type} (disabled)"

    # Extract Body Payload
    body_data = request.get("body") or {}
    content_type = ""
    body_content = ""
    if isinstance(body_data, dict):
        content_type = body_data.get("contentType") or ""
        raw_body = body_data.get("body")
        if isinstance(raw_body, str):
            body_content = _substitute_variables(raw_body.strip(), variables)
        elif raw_body is not None:
            body_content = json.dumps(raw_body, indent=2)

    # Build human & LLM readable text representation
    doc_lines: list[str] = [
        f"API Endpoint: {method} {endpoint}",
        f"Name: {req_name}",
        f"Folder / Collection: {folder_breadcrumb}",
        f"HTTP Method: {method}",
        f"URL: {endpoint}",
        f"Authentication: {auth_display}",
    ]

    description = request.get("description")
    if description:
        doc_lines.append(f"Description: {description.strip()}")

    if headers_list:
        doc_lines.append("Headers:")
        doc_lines.extend(headers_list)

    if params_list:
        doc_lines.append("Query Parameters:")
        doc_lines.extend(params_list)

    if body_content:
        doc_lines.append(f"Request Body ({content_type or 'raw'}):")
        doc_lines.append(body_content)

    content_str = "\n".join(doc_lines)
    title = f"[{method}] {req_name} ({endpoint})" if endpoint else f"[{method}] {req_name}"

    # Stable primary key: hoppscotch:<coll>:<folder_path>:<req_name>
    coll_slug = _slugify(collection_name)
    folder_slug = _slugify(folder_path) if folder_path else "root"
    req_slug = _slugify(req_name)
    primary_key = f"hoppscotch:{coll_slug}:{folder_slug}:{req_slug}"

    return {
        "id": primary_key,
        "title": title,
        "content": content_str,
        "method": method,
        "endpoint": endpoint,
        "collection_name": collection_name,
        "folder_path": folder_path,
        "auth_type": auth_type,
        "content_type": content_type,
        "name": req_name,
    }


def _walk_collection_folders(
    folder: dict[str, Any],
    collection_name: str,
    parent_path: str,
    variables: dict[str, str],
) -> Iterator[dict[str, Any]]:
    """Recursively walk Hoppscotch collection folders yielding formatted requests."""
    current_name = folder.get("name") or ""
    current_path = f"{parent_path}/{current_name}".strip("/") if parent_path else current_name

    # Process requests in this folder
    requests = folder.get("requests") or []
    if isinstance(requests, list):
        for req in requests:
            if isinstance(req, dict):
                yield _format_request_document(
                    req,
                    collection_name=collection_name,
                    folder_path=current_path,
                    variables=variables,
                )

    # Recurse into subfolders
    subfolders = folder.get("folders") or []
    if isinstance(subfolders, list):
        for sub in subfolders:
            if isinstance(sub, dict):
                yield from _walk_collection_folders(
                    sub,
                    collection_name=collection_name,
                    parent_path=current_path,
                    variables=variables,
                )


def iter_hoppscotch_requests(
    source_path: str | os.PathLike,
    environment_path: str | os.PathLike | dict | list | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield normalized API requests from a Hoppscotch export file or directory.

    Args:
        source_path: Path to a Hoppscotch collection JSON export or directory containing JSON exports.
        environment_path: Optional path to an environment JSON export or dictionary of variables.
    """
    path = Path(source_path)
    if not path.exists():
        raise FileNotFoundError(f"Hoppscotch source path does not exist: {path}")

    variables = load_environment_variables(environment_path)

    # Collect files to inspect
    json_files: list[Path] = []
    if path.is_file():
        json_files.append(path)
    elif path.is_dir():
        json_files.extend(sorted(path.glob("**/*.json")))

    if not json_files:
        logger.warning("Hoppscotch connector: no JSON files found in %s", path)
        return

    count = 0
    for file_path in json_files:
        data = _load_json_data(file_path)
        if data is None:
            continue

        # Handle both single collection object and array of collections
        collections = data if isinstance(data, list) else [data]

        for coll in collections:
            if not isinstance(coll, dict):
                continue

            # Check if this JSON object looks like a Hoppscotch collection
            has_requests = "requests" in coll
            has_folders = "folders" in coll
            coll_name = coll.get("name") or file_path.stem

            if not (has_requests or has_folders):
                # Might be an environment export file in the same directory; skip silently
                continue

            # Process top-level requests in collection
            top_requests = coll.get("requests") or []
            if isinstance(top_requests, list):
                for req in top_requests:
                    if isinstance(req, dict):
                        count += 1
                        yield _format_request_document(
                            req,
                            collection_name=coll_name,
                            folder_path="",
                            variables=variables,
                        )

            # Process nested folders
            folders = coll.get("folders") or []
            if isinstance(folders, list):
                for folder in folders:
                    if isinstance(folder, dict):
                        for doc in _walk_collection_folders(
                            folder,
                            collection_name=coll_name,
                            parent_path="",
                            variables=variables,
                        ):
                            count += 1
                            yield doc

    logger.info("Hoppscotch connector: successfully parsed %d endpoint(s) from %s", count, path)


def hoppscotch_source(
    collection_path: str | os.PathLike,
    environment_path: str | os.PathLike | dict | list | None = None,
):
    """Return a ``dlt`` resource over a Hoppscotch collection for :func:`cognee.remember`.

    Args:
        collection_path: Path to the Hoppscotch collection JSON export or directory.
        environment_path: Optional path to an environment JSON file or variables dictionary.

    Returns:
        A ``dlt`` resource (``hoppscotch_requests``) configured with ``write_disposition="replace"``
        and document source marker (``cognee_document_source = "hoppscotch"``).
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Hoppscotch connector requires the dlt extra: pip install "cognee[dlt]".'
        ) from exc

    source_path = str(collection_path)

    @dlt.resource(
        name=HOPPSCOTCH_TABLE_NAME,
        primary_key="id",
        write_disposition="replace",
    )
    def hoppscotch_requests():
        yield from iter_hoppscotch_requests(
            source_path=source_path,
            environment_path=environment_path,
        )

    # Opt into cognee's document ingestion pipeline (message/request -> doc -> cognify)
    setattr(hoppscotch_requests, DOCUMENT_SOURCE_ATTR, HOPPSCOTCH_SOURCE_NAME)
    return hoppscotch_requests
