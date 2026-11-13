"""Format parsed Excalidraw diagrams into structured Markdown and Mermaid graphs."""

import hashlib
import re
from typing import Any

from cognee_community_connector_excalidraw.models import (
    ExcalidrawDiagram,
    RenderOptions,
)

_ESCAPES = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b\n]*(?:\x07|\x1b\\)"
    r"|\x1b[ -/]+[0-~]"
    r"|\x1b[0-Z\\^-~]"
)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _clean(text: str) -> str:
    """Strip ANSI escapes and control characters for database safety."""
    cleaned = _ESCAPES.sub("", text)
    cleaned = _CONTROL.sub("", cleaned)
    return cleaned.encode("utf-8", "replace").decode("utf-8")


def _sanitize_mermaid_id(name: str) -> str:
    """Generate a valid alphanumeric node identifier for Mermaid syntax."""
    clean = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    return clean[:24] or "node"


def render_diagram_markdown(
    diag: ExcalidrawDiagram,
    options: RenderOptions | None = None,
) -> str:
    """Render an ExcalidrawDiagram into high-fidelity Markdown with Mermaid topology."""
    opts = options or RenderOptions()
    lines: list[str] = [
        f"# Diagram: {diag.diagram_name}",
        "",
        "## Overview",
        f"- **File Source:** `{diag.file_path}`",
        f"- **Total Components:** {len(diag.nodes)}",
        f"- **Connected Relationships:** {len(diag.connections)}",
        f"- **Standalone Annotations:** {len(diag.standalone_texts)}",
        "",
    ]

    # Render Mermaid Architecture Graph
    if opts.include_mermaid and (diag.connections or diag.nodes):
        lines.extend(
            [
                "## Architecture Flowchart",
                "```mermaid",
                "flowchart TD",
            ]
        )

        seen_nodes: set[str] = set()

        if diag.connections:
            for conn in diag.connections:
                src_node_id = f"n_{_sanitize_mermaid_id(conn.source_id)}"
                tgt_node_id = f"n_{_sanitize_mermaid_id(conn.target_id)}"

                src_lbl = conn.source_label.replace('"', "'").replace("\n", " ")
                tgt_lbl = conn.target_label.replace('"', "'").replace("\n", " ")

                if src_node_id not in seen_nodes:
                    lines.append(f'    {src_node_id}["{src_lbl}"]')
                    seen_nodes.add(src_node_id)

                if tgt_node_id not in seen_nodes:
                    lines.append(f'    {tgt_node_id}["{tgt_lbl}"]')
                    seen_nodes.add(tgt_node_id)

                if conn.edge_label:
                    edge_lbl = conn.edge_label.replace('"', "'").replace("\n", " ")
                    lines.append(f"    {src_node_id} -->|{edge_lbl}| {tgt_node_id}")
                else:
                    lines.append(f"    {src_node_id} --> {tgt_node_id}")

        if opts.include_isolated_nodes:
            for node in diag.nodes:
                nid = f"n_{_sanitize_mermaid_id(node.id)}"
                if nid not in seen_nodes:
                    lbl = (node.label or "Node").replace('"', "'").replace("\n", " ")
                    lines.append(f'    {nid}["{lbl}"]')
                    seen_nodes.add(nid)

        lines.extend(["```", ""])

    # Relationships Table
    if opts.include_tables and diag.connections:
        lines.extend(
            [
                "## Architectural Relationships",
                "| Source Node | Edge Label | Target Node |",
                "| :--- | :--- | :--- |",
            ]
        )
        for conn in diag.connections:
            s_name = conn.source_label.replace("\n", " ")
            t_name = conn.target_label.replace("\n", " ")
            e_lbl = conn.edge_label.replace("\n", " ") if conn.edge_label else "───"
            lines.append(f"| `{s_name}` | `{e_lbl}` | `{t_name}` |")
        lines.append("")

    # Components Inventory
    if opts.include_tables and diag.nodes:
        lines.extend(
            [
                "## Component Inventory",
                "| Type | Name / Label | Dimensions |",
                "| :--- | :--- | :--- |",
            ]
        )
        for node in sorted(diag.nodes, key=lambda n: (n.type, n.label or "")):
            label_clean = (node.label or "Unnamed").replace("\n", " ")
            dims = f"{int(node.width)}x{int(node.height)}"
            lines.append(f"| `{node.type}` | {label_clean} | {dims} |")
        lines.append("")

    # Standalone Notes & Text
    if diag.standalone_texts:
        lines.extend(["## Annotations & Notes"])
        for txt in diag.standalone_texts:
            clean_txt = txt.replace("\n", "\n  > ")
            lines.append(f"- **Note:**\n  > {clean_txt}")
        lines.append("")

    return _clean("\n".join(lines).strip())


def diagram_to_cognee_document(
    diag: ExcalidrawDiagram,
    options: RenderOptions | None = None,
) -> dict[str, Any]:
    """Convert an ExcalidrawDiagram into a first-class Cognee document payload."""
    markdown_content = render_diagram_markdown(diag, options=options)

    clean_slug = re.sub(r"[^a-zA-Z0-9_-]", "_", diag.diagram_name.lower())
    hasher = hashlib.sha256()
    hasher.update(markdown_content.encode("utf-8"))
    content_hash = hasher.hexdigest()

    return {
        "id": f"excalidraw:{clean_slug}",
        "title": f"Diagram: {diag.diagram_name}",
        "content": markdown_content,
        "url": diag.file_path,
        "source": "excalidraw",
        "data_id": content_hash,
        "nodes_count": len(diag.nodes),
        "connections_count": len(diag.connections),
        "_deleted": False,
        "document_source": True,
    }
