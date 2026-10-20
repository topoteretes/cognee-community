"""Markdown renderer converting Postman collections into structured documents.

Transforms Postman collections, folders, and HTTP request items into structured
Markdown documents conforming to Cognee's document ingestion contract.
Zero emdashes across all code, docstrings, comments, and rendered content.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from cognee_community_connector_postman.models import (
    PostmanCollection,
    PostmanCollectionItem,
    PostmanDocumentRow,
    PostmanResponse,
)

# Deterministic namespace for RFC 4122 UUIDv5 fallback identifiers
POSTMAN_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "postman.cognee.ai")


def _sanitize_text(text: str | None) -> str:
    """Normalize text replacing unicode emdashes and endashes with ASCII hyphens."""
    if not text:
        return ""
    return str(text).replace("\u2014", " - ").replace("\u2013", "-")


def _clean_id_segment(val: str | None) -> str:
    """Sanitize string segment for composite ID: strip whitespace and newlines."""
    if not val:
        return ""
    sanitized = _sanitize_text(val)
    cleaned = re.sub(r"[\r\n]+", "", str(sanitized).strip())
    return re.sub(r"\s+", "_", cleaned)


def _escape_markdown_cell(val: Any) -> str:
    """Escape pipe characters and newlines for Markdown table cells."""
    if val is None:
        return "-"
    s = str(val).strip()
    if not s:
        return "-"
    s = s.replace("\r\n", "<br>").replace("\n", "<br>").replace("\r", "<br>")
    s = s.replace("|", r"\|")
    return _sanitize_text(s)


def generate_fallback_id(
    collection_uid: str,
    breadcrumbs: list[str],
    item_name: str,
    discriminator: str = "",
) -> str:
    """Generate deterministic RFC 4122 UUIDv5 for items lacking explicit IDs."""
    path_str = "/".join(breadcrumbs)
    seed = f"{collection_uid}:{path_str}:{item_name}:{discriminator}"
    return str(uuid.uuid5(POSTMAN_NAMESPACE, seed))


def generate_item_id(
    collection_uid: str,
    item: PostmanCollectionItem | dict[str, Any],
    breadcrumbs: list[str],
    discriminator: str = "",
) -> str:
    """Generate deterministic, stable composite ID for a request or folder item."""
    clean_col = _clean_id_segment(collection_uid) or "default-col"

    explicit_id: str | None = None
    if isinstance(item, PostmanCollectionItem):
        explicit_id = item.id
        item_name = item.name
    elif isinstance(item, dict):
        explicit_id = item.get("id") or item.get("_postman_id")
        item_name = item.get("name", "")
    else:
        item_name = ""

    if explicit_id:
        clean_item = _clean_id_segment(explicit_id)
        if clean_item:
            return f"{clean_col}:{clean_item}"

    fallback_uuid = generate_fallback_id(clean_col, breadcrumbs, item_name, discriminator)
    return f"{clean_col}:{fallback_uuid}"


def generate_overview_id(collection_uid: str, suffix: str = "collection") -> str:
    """Generate stable composite ID for the collection overview document."""
    clean_col = _clean_id_segment(collection_uid) or "default-col"
    clean_sfx = _clean_id_segment(suffix) or "collection"
    return f"{clean_col}:{clean_sfx}"


def build_document_row(
    row_id: str,
    title: str,
    content: str,
    url: str | None = None,
) -> PostmanDocumentRow:
    """Construct a strictly validated PostmanDocumentRow."""
    clean_id = _clean_id_segment(row_id)
    if not clean_id:
        raise ValueError("Document row id cannot be empty.")

    clean_title = _sanitize_text(str(title).strip())
    clean_content = _sanitize_text(str(content).strip())
    clean_url = _sanitize_text(str(url).strip()) if url and str(url).strip() else None

    return {
        "id": clean_id,
        "title": clean_title,
        "content": clean_content,
        "url": clean_url,
    }


def collect_collection_stats(
    collection: PostmanCollection,
) -> tuple[int, int, dict[str, int]]:
    """Calculate total folders, total endpoints, and HTTP method counts.

    Returns:
        tuple of (total_folders, total_requests, method_counts)
    """
    total_folders = 0
    total_requests = 0
    method_counts: dict[str, int] = {}

    def _walk(items: list[PostmanCollectionItem], depth: int = 0) -> None:
        nonlocal total_folders, total_requests
        if depth > 50:
            return
        for item in items:
            if item.is_request:
                total_requests += 1
                method = (item.request.method if item.request else "GET").upper()
                method_counts[method] = method_counts.get(method, 0) + 1
            else:
                total_folders += 1
                _walk(item.item, depth + 1)

    _walk(collection.item)
    return total_folders, total_requests, method_counts


def render_collection_overview(
    collection: PostmanCollection,
    collection_uid: str,
    overview_suffix: str = "collection",
) -> PostmanDocumentRow:
    """Render top-level collection overview markdown document.

    Crucial invariant: Excludes volatile timestamps (updatedAt, createdAt).
    """
    info = collection.info
    name = _sanitize_text(info.name) or "Untitled Collection"
    raw_desc = _sanitize_text(info.description)
    description = raw_desc if raw_desc else "No description provided."

    total_folders, total_requests, method_counts = collect_collection_stats(collection)

    schema_label = "Postman Collection v2.1.0"
    if info.schema and "2.1.0" not in info.schema:
        schema_label = _sanitize_text(info.schema)

    auth_summary = "None / Inherited"
    if collection.auth and isinstance(collection.auth, dict):
        auth_type = collection.auth.get("type", "")
        if auth_type:
            auth_summary = f"{auth_type.capitalize()} Authentication"

    methods_str = (
        ", ".join(f"{k}: {v}" for k, v in sorted(method_counts.items()))
        if method_counts
        else "None"
    )

    metadata_rows = [
        f"| Schema | {schema_label} |",
        f"| Collection ID | {collection_uid} |",
    ]
    if info.version:
        metadata_rows.append(f"| Version | {_sanitize_text(info.version)} |")
    metadata_rows.extend(
        [
            f"| Authentication | {auth_summary} |",
            f"| Total Folders | {total_folders} |",
            f"| Total Endpoints | {total_requests} |",
            f"| Methods | {methods_str} |",
        ]
    )

    lines: list[str] = [
        f"# Collection: {name}",
        "",
        description,
        "",
        "## Metadata",
        "",
        "| Property | Value |",
        "|---|---|",
        *metadata_rows,
    ]

    top_folders = [it for it in collection.item if not it.is_request]
    root_endpoints = [it for it in collection.item if it.is_request]

    if top_folders or root_endpoints:
        lines.extend(["", "## Structure"])
        if top_folders:
            lines.append("### Folders")
            for f in top_folders:
                f_name = _sanitize_text(f.name) or "Unnamed Folder"
                child_reqs = sum(1 for c in f.item if c.is_request)
                child_dirs = sum(1 for c in f.item if not c.is_request)
                lines.append(f"- **{f_name}**: {child_reqs} endpoint(s), {child_dirs} subfolder(s)")
        if root_endpoints:
            lines.append("### Root Endpoints")
            for r in root_endpoints:
                r_method = (r.request.method if r.request else "GET").upper()
                r_name = _sanitize_text(r.name) or "Untitled Request"
                r_desc = _sanitize_text(r.description)
                desc_str = f" : {r_desc}" if r_desc else ""
                lines.append(f"- **[{r_method}] {r_name}**{desc_str}")

    overview_id = generate_overview_id(collection_uid, suffix=overview_suffix)
    overview_title = f"Collection: {name}"

    return build_document_row(
        row_id=overview_id,
        title=overview_title,
        content="\n".join(lines),
        url=None,
    )


def render_folder_document(
    folder: PostmanCollectionItem | dict[str, Any],
    collection_uid: str,
    breadcrumbs: list[str],
    idx: int = 0,
) -> PostmanDocumentRow:
    """Render folder context markdown document.

    Args:
        folder: PostmanCollectionItem instance or raw dictionary.
        collection_uid: Stable collection unique identifier.
        breadcrumbs: Full breadcrumb path including this folder.
        idx: Index among siblings for collision-free fallback ID.
    """
    f_item = (
        folder
        if isinstance(folder, PostmanCollectionItem)
        else PostmanCollectionItem.from_dict(folder)
    )
    folder_name = _sanitize_text(f_item.name) or "Unnamed Folder"
    full_path_str = "Root / " + " / ".join(_sanitize_text(b) for b in breadcrumbs)
    parent_breadcrumbs = breadcrumbs[:-1]
    parent_path_str = (
        "Root / " + " / ".join(_sanitize_text(b) for b in parent_breadcrumbs)
        if parent_breadcrumbs
        else "Root"
    )

    raw_desc = _sanitize_text(f_item.description)
    description = raw_desc if raw_desc else "No folder description provided."

    subfolders = [c for c in f_item.item if not c.is_request]
    direct_endpoints = [c for c in f_item.item if c.is_request]

    lines: list[str] = [
        f"# Folder: {folder_name}",
        "",
        f"**Path**: {full_path_str}",
        f"**Parent**: {parent_path_str}",
        "",
        "## Description",
        "",
        description,
    ]

    lines.extend(["", "## Subfolders", ""])
    if subfolders:
        for sf in subfolders:
            sf_name = _sanitize_text(sf.name) or "Unnamed Subfolder"
            sf_desc = _sanitize_text(sf.description)
            desc_part = f" : {sf_desc}" if sf_desc else ""
            lines.append(f"- **{sf_name}**{desc_part}")
    else:
        lines.append("*No subfolders.*")

    lines.extend(["", "## Endpoints", ""])
    if direct_endpoints:
        for ep in direct_endpoints:
            method = (ep.request.method if ep.request else "GET").upper()
            ep_name = _sanitize_text(ep.name) or "Untitled Endpoint"
            ep_url = _sanitize_text(ep.request.url_str) if ep.request else ""
            ep_desc = _sanitize_text(ep.description)
            url_part = f" (`{ep_url}`)" if ep_url else ""
            desc_part = f" : {ep_desc}" if ep_desc else ""
            lines.append(f"- **[{method}] {ep_name}**{url_part}{desc_part}")
    else:
        lines.append("*No direct endpoints in this folder.*")

    folder_id = generate_item_id(
        collection_uid=collection_uid,
        item=f_item,
        breadcrumbs=parent_breadcrumbs,
        discriminator=f"folder:{idx}",
    )
    breadcrumb_title = " / ".join(_sanitize_text(b) for b in breadcrumbs)

    return build_document_row(
        row_id=folder_id,
        title=f"Folder: {breadcrumb_title}",
        content="\n".join(lines),
        url=None,
    )


def _render_headers_table(headers: list[dict[str, Any]] | None) -> str | None:
    """Render Markdown table for active HTTP request headers."""
    if not headers or not isinstance(headers, list):
        return None
    active = [h for h in headers if isinstance(h, dict) and not h.get("disabled")]
    if not active:
        return None
    lines = [
        "| Header | Value | Description |",
        "|---|---|---|",
    ]
    for h in active:
        k = _escape_markdown_cell(h.get("key", ""))
        v = _escape_markdown_cell(h.get("value", ""))
        desc = _escape_markdown_cell(h.get("description"))
        lines.append(f"| {k} | {v} | {desc} |")
    return "\n".join(lines)


def _render_query_params_table(query_params: list[dict[str, Any]] | None) -> str | None:
    """Render Markdown table for active URL query parameters."""
    if not query_params or not isinstance(query_params, list):
        return None
    active = [q for q in query_params if isinstance(q, dict) and not q.get("disabled")]
    if not active:
        return None
    lines = [
        "| Query Param | Value | Description |",
        "|---|---|---|",
    ]
    for q in active:
        k = _escape_markdown_cell(q.get("key", ""))
        v = _escape_markdown_cell(q.get("value", ""))
        desc = _escape_markdown_cell(q.get("description"))
        lines.append(f"| {k} | {v} | {desc} |")
    return "\n".join(lines)


def _render_path_variables_table(variables: list[dict[str, Any]] | None) -> str | None:
    """Render Markdown table for active URL path variables."""
    if not variables or not isinstance(variables, list):
        return None
    active = [v for v in variables if isinstance(v, dict) and not v.get("disabled")]
    if not active:
        return None
    lines = [
        "| Variable | Value | Description |",
        "|---|---|---|",
    ]
    for v in active:
        k = _escape_markdown_cell(v.get("key", ""))
        val = _escape_markdown_cell(v.get("value", ""))
        desc = _escape_markdown_cell(v.get("description"))
        lines.append(f"| {k} | {val} | {desc} |")
    return "\n".join(lines)


def _render_raw_body(body_obj: dict[str, Any]) -> str | None:
    """Render raw payload string with syntax highlighting and pretty indentation."""
    raw = body_obj.get("raw")
    if raw is None or not str(raw).strip():
        return None
    raw_str = str(raw)
    options = body_obj.get("options")
    language = ""
    if isinstance(options, dict):
        raw_opt = options.get("raw")
        if isinstance(raw_opt, dict):
            language = str(raw_opt.get("language") or "")
        elif isinstance(raw_opt, str):
            language = raw_opt

    is_json = language == "json" or (not language and raw_str.strip().startswith(("{", "[")))
    if is_json:
        try:
            parsed = json.loads(raw_str)
            formatted = json.dumps(parsed, indent=2)
            return f"Body:\n```json\n{_sanitize_text(formatted)}\n```"
        except (json.JSONDecodeError, ValueError, TypeError):
            return f"Body:\n```\n{_sanitize_text(raw_str)}\n```"

    if language:
        return f"Body:\n```{language}\n{_sanitize_text(raw_str)}\n```"
    return f"Body:\n```\n{_sanitize_text(raw_str)}\n```"


def _render_urlencoded_body(body_obj: dict[str, Any]) -> str | None:
    """Render x-www-form-urlencoded parameters list and table."""
    items = body_obj.get("urlencoded", [])
    if not isinstance(items, list):
        return None
    active = [p for p in items if isinstance(p, dict) and not p.get("disabled")]
    if not active:
        return None

    kv_list = "\n".join(
        [
            f"- {_escape_markdown_cell(p.get('key', ''))}: "
            f"{_escape_markdown_cell(p.get('value', ''))}"
            for p in active
        ]
    )
    lines = [
        "| Parameter | Value | Description |",
        "|---|---|---|",
    ]
    for p in active:
        k = _escape_markdown_cell(p.get("key", ""))
        v = _escape_markdown_cell(p.get("value", ""))
        desc = _escape_markdown_cell(p.get("description"))
        lines.append(f"| {k} | {v} | {desc} |")

    return f"Body:\n{kv_list}\n\n" + "\n".join(lines)


def _render_formdata_body(body_obj: dict[str, Any]) -> str | None:
    """Render multipart form data list and table."""
    items = body_obj.get("formdata", [])
    if not isinstance(items, list):
        return None
    active = [f for f in items if isinstance(f, dict) and not f.get("disabled")]
    if not active:
        return None

    kv_list = "\n".join(
        [
            f"- {_escape_markdown_cell(f.get('key', ''))}: "
            f"{_escape_markdown_cell(f.get('value', ''))}"
            for f in active
        ]
    )
    lines = [
        "| Field | Type | Value | Description |",
        "|---|---|---|---|",
    ]
    for f in active:
        k = _escape_markdown_cell(f.get("key", ""))
        ft = _escape_markdown_cell(f.get("type", "text"))
        v = _escape_markdown_cell(f.get("value", ""))
        desc = _escape_markdown_cell(f.get("description"))
        lines.append(f"| {k} | {ft} | {v} | {desc} |")

    return f"Body:\n{kv_list}\n\n" + "\n".join(lines)


def _render_graphql_body(body_obj: dict[str, Any]) -> str | None:
    """Render GraphQL query and variables blocks."""
    gql = body_obj.get("graphql", {})
    if not isinstance(gql, dict):
        return None

    raw_query = gql.get("query")
    raw_vars = gql.get("variables")
    query_str = str(raw_query).strip() if raw_query is not None else ""
    variables_str = str(raw_vars).strip() if raw_vars is not None else ""
    parts: list[str] = []

    if query_str:
        parts.append(f"Query:\n```graphql\n{_sanitize_text(query_str)}\n```")
    if variables_str:
        try:
            parsed_v = json.loads(variables_str)
            fmt_v = json.dumps(parsed_v, indent=2)
            parts.append(f"Variables:\n```json\n{_sanitize_text(fmt_v)}\n```")
        except (json.JSONDecodeError, ValueError, TypeError):
            parts.append(f"Variables:\n```\n{_sanitize_text(variables_str)}\n```")

    if not parts:
        return None
    return "Body:\n" + "\n\n".join(parts)


def _render_request_body(body_obj: dict[str, Any] | None) -> str | None:
    """Dispatch request body formatting across supported modes."""
    if not isinstance(body_obj, dict) or body_obj.get("disabled"):
        return None
    mode = body_obj.get("mode")
    if not mode or not isinstance(mode, str):
        return None

    mode_clean = mode.lower().strip()
    if mode_clean == "raw":
        return _render_raw_body(body_obj)
    if mode_clean == "urlencoded":
        return _render_urlencoded_body(body_obj)
    if mode_clean == "formdata":
        return _render_formdata_body(body_obj)
    if mode_clean == "graphql":
        return _render_graphql_body(body_obj)
    return None


def _render_sample_responses(
    responses: list[PostmanResponse | dict[str, Any]] | None,
) -> str | None:
    """Render sample responses with status codes, headers, and bodies."""
    if not responses or not isinstance(responses, list):
        return None

    resp_parts: list[str] = ["### Sample Responses"]
    for r in responses:
        if isinstance(r, PostmanResponse):
            code = r.code if r.code is not None else 200
            name = r.name or "Response"
            body = r.body or ""
            headers = r.header
        elif isinstance(r, dict):
            code = r.get("code", 200)
            name = r.get("name", "Response")
            body = r.get("body", "")
            headers = r.get("header", [])
        else:
            continue

        clean_name = _sanitize_text(name)
        header_text = ""
        if headers and isinstance(headers, list):
            h_lines = ["Headers:", "| Header | Value |", "|---|---|"]
            for h in headers:
                if isinstance(h, dict):
                    hk = _escape_markdown_cell(h.get("key", ""))
                    hv = _escape_markdown_cell(h.get("value", ""))
                    h_lines.append(f"| {hk} | {hv} |")
            if len(h_lines) > 3:
                header_text = "\n" + "\n".join(h_lines) + "\n"

        body_str = str(body) if body is not None else ""
        if body_str.strip().startswith(("{", "[")):
            try:
                parsed_b = json.loads(body_str)
                fmt_b = json.dumps(parsed_b, indent=2)
                body_block = f"```json\n{_sanitize_text(fmt_b)}\n```"
            except (json.JSONDecodeError, ValueError, TypeError):
                body_block = f"```\n{_sanitize_text(body_str)}\n```"
        else:
            body_block = f"```\n{_sanitize_text(body_str)}\n```"

        resp_parts.append(f"- Status {code} ({clean_name}):{header_text}\n{body_block}")

    return "\n\n".join(resp_parts) if len(resp_parts) > 1 else None


def render_request_document(
    item: PostmanCollectionItem | dict[str, Any],
    collection_uid: str,
    breadcrumbs: list[str],
    idx: int = 0,
    seen_ids: set[str] | None = None,
) -> PostmanDocumentRow:
    """Render a Postman request item into a Cognee document row."""
    req_item = (
        item if isinstance(item, PostmanCollectionItem) else PostmanCollectionItem.from_dict(item)
    )
    req = req_item.request
    method = (req.method if req else "GET").upper().strip()
    name = (req_item.name or "").strip() or "Untitled Request"
    url_str = req.url_str if req else ""

    folder_str = " / ".join(_sanitize_text(b) for b in breadcrumbs) if breadcrumbs else "Root"
    content_parts: list[str] = [
        f"## {method} {_sanitize_text(name)}",
        f"Folder: {folder_str}",
        f"Endpoint: {_sanitize_text(url_str)}",
    ]

    desc = req_item.description or (req.description if req else None)
    if desc and str(desc).strip():
        content_parts.append(f"Description: {_sanitize_text(str(desc).strip())}")

    if req:
        headers_table = _render_headers_table(req.header)
        if headers_table:
            content_parts.append(headers_table)

        query_table = _render_query_params_table(req.url.query)
        if query_table:
            content_parts.append(query_table)

        vars_table = _render_path_variables_table(req.url.variable)
        if vars_table:
            content_parts.append(vars_table)

        body_block = _render_request_body(req.body)
        if body_block:
            content_parts.append(body_block)

    responses_block = _render_sample_responses(req_item.response)
    if responses_block:
        content_parts.append(responses_block)

    full_content = "\n\n".join(content_parts)

    discriminator = f"{method}:{url_str}:{idx}"
    row_id = generate_item_id(
        collection_uid=collection_uid,
        item=req_item,
        breadcrumbs=breadcrumbs,
        discriminator=discriminator,
    )
    if seen_ids is not None:
        orig_id = row_id
        suffix_idx = idx
        while row_id in seen_ids:
            row_id = f"{orig_id}_{suffix_idx}"
            suffix_idx += 1
        seen_ids.add(row_id)

    title = f"[{method}] {_sanitize_text(name)}"
    doc_url = url_str if url_str.strip() else None

    return build_document_row(
        row_id=row_id,
        title=title,
        content=full_content,
        url=doc_url,
    )


def render_collection(
    collection: PostmanCollection,
    collection_uid: str,
    *,
    overview_suffix: str = "collection",
    include_folders: bool = False,
) -> list[PostmanDocumentRow]:
    """Render a PostmanCollection model into structured document rows.

    Args:
        collection: Strongly typed PostmanCollection model instance.
        collection_uid: Stable collection unique identifier.
        overview_suffix: Suffix for overview ID ("collection" or "overview").
        include_folders: Whether to emit separate document rows for folders.

    Returns:
        List of PostmanDocumentRow dictionaries conforming to Cognee contract.
    """
    rows: list[PostmanDocumentRow] = []
    seen_ids: set[str] = set()

    # 1. Collection Overview Document
    overview_row = render_collection_overview(
        collection=collection,
        collection_uid=collection_uid,
        overview_suffix=overview_suffix,
    )
    rows.append(overview_row)
    seen_ids.add(overview_row["id"])

    # 2. Recursive Traversal of Items
    def _traverse(
        items: list[PostmanCollectionItem],
        breadcrumbs: list[str],
        depth: int = 0,
    ) -> None:
        if depth > 50:
            return
        for idx, item in enumerate(items):
            if item.is_request:
                req_row = render_request_document(
                    item=item,
                    collection_uid=collection_uid,
                    breadcrumbs=breadcrumbs,
                    idx=idx,
                    seen_ids=seen_ids,
                )
                rows.append(req_row)
            else:
                current_breadcrumbs = [*breadcrumbs, item.name or "Unnamed Folder"]
                if include_folders:
                    folder_row = render_folder_document(
                        folder=item,
                        collection_uid=collection_uid,
                        breadcrumbs=current_breadcrumbs,
                        idx=idx,
                    )
                    orig_fid = folder_row["id"]
                    suffix_idx = idx
                    while folder_row["id"] in seen_ids:
                        folder_row["id"] = f"{orig_fid}_{suffix_idx}"
                        suffix_idx += 1
                    seen_ids.add(folder_row["id"])
                    rows.append(folder_row)

                _traverse(item.item, current_breadcrumbs, depth + 1)

    _traverse(collection.item, [], 0)
    return rows


def render_collection_documents(
    collection_json: dict[str, Any],
    collection_uid: str | None = None,
    *,
    overview_suffix: str = "collection",
    include_folders: bool = False,
) -> list[PostmanDocumentRow]:
    """Public facade converting raw collection JSON into Cognee document rows.

    Args:
        collection_json: Raw Postman collection JSON (v2.1.0).
        collection_uid: Optional collection UID. Inferred from info if omitted.
        overview_suffix: Suffix for overview ID ("collection" or "overview").
        include_folders: Whether to emit separate document rows for folders.

    Returns:
        List of PostmanDocumentRow dictionaries ready for Cognee ingestion.
    """
    if not isinstance(collection_json, dict):
        return []

    collection = PostmanCollection.from_dict(collection_json)
    resolved_uid = collection_uid or collection.info.postman_id or "default-col"

    return render_collection(
        collection=collection,
        collection_uid=str(resolved_uid),
        overview_suffix=overview_suffix,
        include_folders=include_folders,
    )
