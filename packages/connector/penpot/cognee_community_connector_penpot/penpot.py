"""DLT source for Penpot open-source design platform.

Ingests Penpot design files, boards, text layers, component instances, and team comment threads
into structured Cognee documents for cross-disciplinary product recall and UI decision synthesis.

Features:
- Compatibility with both design.penpot.app and self-hosted Penpot instances via token auth
- Ingestion of design pages, board structures, UI copy / text layers, and component instances
- Ingestion of team design discussions and comment threads with resolution status tracking
- Strict privacy allowlist: extracts text and component names only (excludes raw heavy binary assets/geometry)
- Document-mode registration (DOCUMENT_SOURCE_ATTR = "penpot") for entity linking in Cognee
- Replace-mode synchronization with orphan cleanup
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
from typing import Any

try:
    from cognee.shared.logging_utils import get_logger
    logger = get_logger("penpot_connector")
except ImportError:
    logger = logging.getLogger("penpot_connector")

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

PENPOT_SOURCE_NAME = "penpot"
PENPOT_TABLE_NAME = "penpot_designs"
DEFAULT_PENPOT_BASE_URL = "https://design.penpot.app"


def parse_penpot_page_objects(
    page_data: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Extract boards, text layer contents, and component instances from a Penpot page object.

    Returns:
        (boards_list, all_text_layers, all_components)
    """
    objects = page_data.get("objects") or {}
    if isinstance(objects, list):
        # Flattened object array
        obj_dict = {o.get("id", str(idx)): o for idx, o in enumerate(objects) if isinstance(o, dict)}
    elif isinstance(objects, dict):
        obj_dict = objects
    else:
        obj_dict = {}

    boards: list[dict[str, Any]] = []
    text_layers: list[str] = []
    components: list[str] = []

    for obj_id, obj in obj_dict.items():
        if not isinstance(obj, dict):
            continue

        obj_type = obj.get("type", "")
        obj_name = obj.get("name", "Layer")

        # Board / Frame detection
        if obj_type in ("frame", "board", "group"):
            boards.append({
                "id": obj_id,
                "name": obj_name,
                "type": obj_type,
            })

        # Text layer detection
        if obj_type == "text" or "text" in obj:
            raw_text = obj.get("text") or obj.get("content") or ""
            if isinstance(raw_text, str) and raw_text.strip():
                text_layers.append(raw_text.strip())

        # Component instance detection
        if obj.get("componentId") or obj.get("component-id") or obj_type == "component":
            comp_name = obj.get("componentName") or obj.get("name") or "Component"
            components.append(comp_name)

    return boards, text_layers, components


def format_page_document_markdown(
    file_name: str,
    page_name: str,
    project_name: str,
    boards: list[dict[str, Any]],
    text_layers: list[str],
    components: list[str],
) -> str:
    """Render a Penpot design page into structured Markdown with boards, text copy, and components."""
    lines = [
        f"# [Penpot Design Page] {file_name} / {page_name}",
        "",
        "## File & Page Hierarchy",
        f"- **Project:** `{project_name}`",
        f"- **Design File:** `{file_name}`",
        f"- **Page Name:** `{page_name}`",
        f"- **Total Boards / Screens:** {len(boards)}",
        f"- **Unique Component Types:** {len(set(components))}",
        "",
        "## Boards & Screen Architecture",
    ]

    if boards:
        for b in boards:
            lines.append(f"- **Screen / Board:** `{b['name']}` ({b.get('type', 'frame')})")
    else:
        lines.append("- _No distinct boards configured on this page._")

    lines.extend([
        "",
        "## Reusable Components Used",
    ])
    if components:
        for comp in sorted(set(components)):
            lines.append(f"- Instance of `{comp}`")
    else:
        lines.append("- _No external component instances detected._")

    lines.extend([
        "",
        "## Screen Text & UI Copy",
    ])
    if text_layers:
        for text in text_layers:
            lines.append(f"- \"{text}\"")
    else:
        lines.append("- _No text layers found._")

    return "\n".join(lines)


def format_comment_thread_markdown(
    file_name: str,
    thread_data: dict[str, Any],
) -> str:
    """Render a Penpot comment thread with replies and resolution status into Markdown."""
    thread_id = thread_data.get("id", "thread")
    is_resolved = bool(thread_data.get("isResolved") or thread_data.get("is_resolved", False))
    comments = thread_data.get("comments") or []
    board_name = thread_data.get("boardName") or thread_data.get("pageName") or "General Screen"

    res_badge = "✅ **RESOLVED**" if is_resolved else "💬 **ACTIVE / OPEN**"

    lines = [
        f"# [Penpot Discussion] {file_name} — {board_name}",
        "",
        "## Discussion Status & Context",
        f"- **Thread ID:** `{thread_id}`",
        f"- **Status:** {res_badge}",
        f"- **Design File:** `{file_name}`",
        f"- **Attached Screen / Board:** `{board_name}`",
        f"- **Total Replies:** {len(comments)}",
        "",
        "## Conversation Transcript",
        "",
    ]

    if comments and isinstance(comments, list):
        for c in comments:
            if isinstance(c, dict):
                author = c.get("ownerDisplayName") or c.get("author") or "Team Member"
                date_str = c.get("createdAt") or c.get("date") or "Unknown Date"
                body = c.get("text") or c.get("content") or ""
                lines.extend([
                    f"### {author} ({date_str})",
                    "",
                    f"> {body.strip()}",
                    "",
                ])
    else:
        first_msg = thread_data.get("text") or "No comment body."
        lines.append(f"> {first_msg}")

    return "\n".join(lines)


def penpot_source(
    auth_token: str | None = None,
    base_url: str = DEFAULT_PENPOT_BASE_URL,
    project_ids: list[str] | str | None = None,
    snapshot_data: dict[str, Any] | list[dict[str, Any]] | str | Path | None = None,
    client: Any = None,
):
    """Create a dlt source yielding Penpot design pages and discussions as searchable Cognee documents.

    Args:
        auth_token: Penpot Personal Access Token (defaults to PENPOT_TOKEN env var).
        base_url: Penpot API base URL (defaults to https://design.penpot.app).
        project_ids: Optional list of project IDs to sync.
        snapshot_data: Optional pre-fetched design file dict, list of files, or JSON file path.
        client: Optional custom callable client for tests or custom RPC wrappers.

    Returns:
        A dlt source configured for Cognee document ingestion.
    """
    # Pre-validate file path if snapshot path provided
    if isinstance(snapshot_data, (str, Path)):
        p = Path(snapshot_data)
        if not p.exists():
            raise FileNotFoundError(f"Penpot snapshot file not found: {p}")

    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Penpot connector requires dlt: pip install "cognee-community-connector-penpot" '
            'or pip install "dlt[sqlalchemy]".'
        ) from exc

    resolved_files: list[dict[str, Any]] = []

    # 1. Process snapshot data if provided
    if snapshot_data is not None:
        if isinstance(snapshot_data, dict):
            resolved_files.append(snapshot_data)
        elif isinstance(snapshot_data, list):
            resolved_files.extend([item for item in snapshot_data if isinstance(item, dict)])
        elif isinstance(snapshot_data, (str, Path)):
            p = Path(snapshot_data)
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                resolved_files.append(raw)
            elif isinstance(raw, list):
                resolved_files.extend([item for item in raw if isinstance(item, dict)])

    # 2. Process live Penpot API if token/client provided
    elif auth_token or os.environ.get("PENPOT_TOKEN") or client:
        token = auth_token or os.environ.get("PENPOT_TOKEN") or ""
        if client:
            resolved_files = client(project_ids)
        else:
            import requests

            headers = {
                "Authorization": f"Token {token}",
                "Accept": "application/json",
                "User-Agent": "Cognee-Penpot-Connector/0.1",
            }
            # Example RPC invocation for projects
            # Method call structure: POST /api/main/methods/get-project-files with {"project-id": ...}
            url = f"{base_url.rstrip('/')}/api/main/methods/get-project-files"
            proj_list = [project_ids] if isinstance(project_ids, str) else (project_ids or [])
            for pid in proj_list:
                try:
                    resp = requests.post(url, headers=headers, json={"project-id": pid}, timeout=15.0)
                    resp.raise_for_status()
                    files = resp.json()
                    if isinstance(files, list):
                        resolved_files.extend(files)
                except Exception as exc:
                    logger.warning("Penpot: Error fetching files for project %s: %s", pid, exc)
                    raise

    if not resolved_files:
        raise ValueError("No valid Penpot files or snapshot data provided via snapshot_data= or auth_token=.")

    flattened_documents: list[dict[str, Any]] = []

    for file_record in resolved_files:
        file_id = file_record.get("id", "file")
        file_name = file_record.get("name", "Design File")
        project_name = file_record.get("projectName") or file_record.get("project_name") or "Main Project"

        # Ingest Pages
        pages = file_record.get("pages") or []
        if isinstance(pages, dict):
            pages = list(pages.values())

        for idx, page in enumerate(pages):
            page_id = page.get("id") or f"page_{idx}"
            page_name = page.get("name") or f"Page {idx + 1}"
            boards, text_layers, components = parse_penpot_page_objects(page)
            markdown_content = format_page_document_markdown(
                file_name=file_name,
                page_name=page_name,
                project_name=project_name,
                boards=boards,
                text_layers=text_layers,
                components=components,
            )

            doc_id = f"penpot_page_{file_id}_{page_id}"
            doc = {
                "id": doc_id,
                "title": f"[Penpot Design] {file_name} / {page_name}",
                "url": f"{base_url.rstrip('/')}/#/workspace/{file_id}/{page_id}",
                "file_name": file_name,
                "page_name": page_name,
                "project_name": project_name,
                "board_count": len(boards),
                "component_count": len(components),
                "cognee_node_set": [
                    "penpot",
                    f"file:{file_name.lower().replace(' ', '_')}",
                    "design",
                    "ui_components",
                ],
                "content": markdown_content,
            }
            flattened_documents.append(doc)

        # Ingest Comment Threads
        threads = file_record.get("threads") or file_record.get("comments") or []
        for thread in threads:
            if not isinstance(thread, dict):
                continue
            thread_id = thread.get("id", "thread")
            thread_md = format_comment_thread_markdown(file_name, thread)
            doc_id = f"penpot_thread_{file_id}_{thread_id}"

            doc = {
                "id": doc_id,
                "title": f"[Penpot Discussion] {file_name} / Thread #{thread_id}",
                "url": f"{base_url.rstrip('/')}/#/workspace/{file_id}?thread={thread_id}",
                "file_name": file_name,
                "is_resolved": bool(thread.get("isResolved", False)),
                "cognee_node_set": [
                    "penpot",
                    f"file:{file_name.lower().replace(' ', '_')}",
                    "design_feedback",
                    "comments",
                ],
                "content": thread_md,
            }
            flattened_documents.append(doc)

    @dlt.resource(name=PENPOT_TABLE_NAME, primary_key="id", write_disposition="replace")
    def penpot_designs():
        for doc in flattened_documents:
            yield doc

    @dlt.source(name=PENPOT_SOURCE_NAME)
    def _penpot():
        return penpot_designs

    source = _penpot()
    setattr(source, DOCUMENT_SOURCE_ATTR, PENPOT_SOURCE_NAME)
    return source
