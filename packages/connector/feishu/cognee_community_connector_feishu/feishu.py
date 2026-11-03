"""Feishu / Lark Docs & Wiki data-source connector for cognee.

Extracts rich team documents, specifications, and wiki knowledge spaces from
Feishu (open.feishu.cn) or Lark (open.larksuite.com) into clean semantic markdown
documents for Cognee graph ingestion and agent RAG memory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Generator, Iterable, List, Optional, Union

DOCUMENT_SOURCE_ATTR = "feishu"

DOMAIN_API_URLS = {
    "feishu": "https://open.feishu.cn",
    "lark": "https://open.larksuite.com",
}


def _extract_text_elements(elements: List[Dict[str, Any]]) -> str:
    """Convert Feishu rich text element array into plain or inline markdown string."""
    parts = []
    for el in elements:
        text_run = el.get("text_run", {})
        content = text_run.get("content", "")
        style = text_run.get("text_element_style", {})

        if not content:
            continue

        # Inline formatting
        if style.get("inline_code"):
            content = f"`{content}`"
        if style.get("bold"):
            content = f"**{content}**"
        if style.get("italic"):
            content = f"*{content}*"
        if style.get("link"):
            url = style["link"].get("url", "")
            if url:
                content = f"[{content}]({url})"

        parts.append(content)
    return "".join(parts)


def convert_docx_blocks_to_markdown(blocks: List[Dict[str, Any]]) -> str:
    """Parse Feishu Docx block tree into structured markdown."""
    md_lines: List[str] = []

    for block in blocks:
        btype = block.get("block_type")
        # Block type mappings per Feishu Docx API
        # 1: Page root, 2: Text, 3: H1, 4: H2, 5: H3, 6: H4, 7: H5, 8: H6
        # 11: Bullet, 12: Ordered, 14: Code, 15: Quote, 17: Todo, 18: Callout

        if btype == 2:  # Text
            text_el = block.get("text", {}).get("elements", [])
            txt = _extract_text_elements(text_el)
            if txt:
                md_lines.append(txt)
                md_lines.append("")
        elif btype == 3:  # H1
            text_el = block.get("heading1", {}).get("elements", [])
            md_lines.append(f"# {_extract_text_elements(text_el)}")
            md_lines.append("")
        elif btype == 4:  # H2
            text_el = block.get("heading2", {}).get("elements", [])
            md_lines.append(f"## {_extract_text_elements(text_el)}")
            md_lines.append("")
        elif btype == 5:  # H3
            text_el = block.get("heading3", {}).get("elements", [])
            md_lines.append(f"### {_extract_text_elements(text_el)}")
            md_lines.append("")
        elif btype in (6, 7, 8):  # H4-H6
            h_level = "#" * (btype - 2)
            key = f"heading{btype - 2}"
            text_el = block.get(key, {}).get("elements", [])
            md_lines.append(f"{h_level} {_extract_text_elements(text_el)}")
            md_lines.append("")
        elif btype == 11 or btype == 12:  # Bullet list or ordered list
            bullet_el = block.get("bullet", {}).get("elements", []) or block.get("ordered", {}).get("elements", [])
            txt = _extract_text_elements(bullet_el)
            prefix = "- " if btype == 11 else "1. "
            md_lines.append(f"{prefix}{txt}")
        elif btype == 14:  # Code block
            code_obj = block.get("code", {})
            elements = code_obj.get("elements", [])
            lang = code_obj.get("style", {}).get("language", 0)
            lang_str = "python" if lang == 1 else ""
            raw_code = "".join(el.get("text_run", {}).get("content", "") for el in elements)
            md_lines.append(f"```{lang_str}\n{raw_code}\n```")
            md_lines.append("")
        elif btype == 15:  # Quote
            quote_el = block.get("quote", {}).get("elements", [])
            txt = _extract_text_elements(quote_el)
            md_lines.append(f"> {txt}")
            md_lines.append("")
        elif btype == 17:  # Todo
            todo_obj = block.get("todo", {})
            todo_el = todo_obj.get("elements", [])
            done = todo_obj.get("style", {}).get("done", False)
            checkbox = "[x]" if done else "[ ]"
            txt = _extract_text_elements(todo_el)
            md_lines.append(f"- {checkbox} {txt}")
        elif btype == 18:  # Callout
            callout_el = block.get("callout", {}).get("elements", [])
            txt = _extract_text_elements(callout_el)
            md_lines.append(f"> [!NOTE]\n> {txt}")
            md_lines.append("")

    return "\n".join(md_lines).strip()


def format_feishu_document(
    doc: Dict[str, Any],
    domain: str = "feishu",
) -> str:
    """Render a Feishu/Lark document and metadata into clean agent-queryable markdown."""
    doc_id = doc.get("document_id") or doc.get("obj_token") or "doc_unknown"
    title = doc.get("title") or "Untitled Document"
    space_name = doc.get("space_name") or "Team Workspace"
    space_id = doc.get("space_id") or "global"
    author = doc.get("author") or "Team Member"
    updated_at = doc.get("updated_at") or doc.get("create_time") or "N/A"

    blocks = doc.get("blocks", [])
    if blocks:
        body_content = convert_docx_blocks_to_markdown(blocks)
    else:
        body_content = doc.get("content", "No body content available.")

    lines = [
        f"# [Feishu Document] {title}",
        "",
        "## Document Metadata",
        f"- **Document ID:** `{doc_id}`",
        f"- **Title:** `{title}`",
        f"- **Wiki Space:** `{space_name}` (ID: `{space_id}`)",
        f"- **Author / Owner:** `{author}`",
        f"- **Last Updated:** `{updated_at}`",
        f"- **Platform Domain:** `{domain}`",
        "",
        "## Content",
        "",
        body_content,
    ]

    return "\n".join(lines)


def parse_feishu_workspace(
    data: Dict[str, Any],
    domain: str = "feishu",
) -> List[Dict[str, Any]]:
    """Extract documents and node sets from Feishu/Lark workspace payload."""
    documents = []
    docs = data.get("documents", [])

    for doc in docs:
        doc_id = doc.get("document_id") or doc.get("obj_token") or "doc_unknown"
        space_id = doc.get("space_id") or "default_space"
        title = doc.get("title") or "Untitled Document"

        doc_text = format_feishu_document(doc, domain=domain)
        content_hash = hashlib.sha256(doc_text.encode("utf-8")).hexdigest()

        node_sets = [f"feishu:doc:{doc_id}"]
        if space_id:
            node_sets.append(f"feishu:space:{space_id}")

        documents.append({
            "id": f"feishu_{domain}_{doc_id}",
            "type": "document",
            "entity_id": doc_id,
            "text": doc_text,
            "hash": content_hash,
            "node_sets": node_sets,
            "metadata": {
                "source": DOCUMENT_SOURCE_ATTR,
                "domain": domain,
                "document_id": doc_id,
                "space_id": space_id,
                "title": title,
            },
        })

    return documents


def feishu_source(
    app_id: str = "",
    app_secret: str = "",
    space_ids: Optional[List[str]] = None,
    document_ids: Optional[List[str]] = None,
    domain: str = "feishu",
    snapshot_data: Optional[Union[Dict[str, Any], str, Path]] = None,
) -> Any:
    """Cognee DLT source for indexing Feishu / Lark Docs & Wiki into agent semantic memory."""
    if isinstance(snapshot_data, (str, Path)):
        p = Path(snapshot_data)
        if not p.exists():
            raise FileNotFoundError(f"Feishu snapshot file not found: {p}")
        with open(p, "r", encoding="utf-8") as f:
            raw_payload = json.load(f)
    elif isinstance(snapshot_data, dict):
        raw_payload = snapshot_data
    elif app_id and app_secret:
        import requests

        base_url = DOMAIN_API_URLS.get(domain, DOMAIN_API_URLS["feishu"])

        # 1. Acquire tenant access token
        auth_url = f"{base_url}/open-apis/auth/v3/tenant_access_token/internal"
        auth_resp = requests.post(
            auth_url,
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=15,
        )
        auth_resp.raise_for_status()
        auth_data = auth_resp.json()
        if auth_data.get("code") != 0:
            raise RuntimeError(f"Feishu authentication failed: {auth_data.get('msg')}")

        token = auth_data["tenant_access_token"]
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }

        docs_list: List[Dict[str, Any]] = []

        # 2. If space_ids provided, list wiki nodes
        target_docs = list(document_ids or [])
        if space_ids:
            for sid in space_ids:
                nodes_url = f"{base_url}/open-apis/wiki/v2/spaces/{sid}/nodes"
                nodes_resp = requests.get(nodes_url, headers=headers, timeout=25)
                if nodes_resp.status_code == 200:
                    items = nodes_resp.json().get("data", {}).get("items", [])
                    for item in items:
                        if item.get("obj_type") == "docx":
                            target_docs.append(item.get("obj_token"))

        # Deduplicate doc tokens
        unique_docs = sorted(list(set(target_docs)))

        # 3. Fetch doc blocks for each target doc
        for did in unique_docs:
            blocks_url = f"{base_url}/open-apis/docx/v1/documents/{did}/blocks"
            b_resp = requests.get(blocks_url, headers=headers, timeout=30)
            if b_resp.status_code == 200:
                blocks = b_resp.json().get("data", {}).get("items", [])
                docs_list.append({
                    "document_id": did,
                    "title": f"Doc {did}",
                    "blocks": blocks,
                })

        raw_payload = {"documents": docs_list}
    else:
        raise ValueError("Must provide either 'snapshot_data' or both 'app_id' and 'app_secret'.")

    parsed_docs = parse_feishu_workspace(raw_payload, domain=domain)

    try:
        import dlt
    except ImportError as e:
        raise ImportError(
            "The Feishu connector requires dlt: pip install 'cognee-community-connector-feishu' or pip install 'dlt[sqlalchemy]'."
        ) from e

    @dlt.source(name="feishu")
    def _source() -> Generator[Any, None, None]:
        @dlt.resource(name="documents", write_disposition="replace")
        def _documents() -> Generator[Dict[str, Any], None, None]:
            for doc in parsed_docs:
                yield {
                    "id": doc["id"],
                    "text": doc["text"],
                    "cognee_document_source": DOCUMENT_SOURCE_ATTR,
                    "cognee_node_set": doc.get("node_sets", []),
                    "metadata": doc.get("metadata", {}),
                }

        yield _documents()

    return _source()
