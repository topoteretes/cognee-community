"""Parser for converting Excalidraw JSON payloads into structural diagrams."""

import json
import re
from pathlib import Path
from typing import Any

from cognee_community_connector_excalidraw.models import (
    ExcalidrawConnection,
    ExcalidrawDiagram,
    ExcalidrawElement,
)

ANSI_ESCAPE_PATTERN = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
CONTROL_CHARS_PATTERN = re.compile(r"[\x00-\x08\x0B-\x0C\x0E-\x1F\x7F]")


def _clean_text(val: str | None) -> str | None:
    """Strip ANSI color escapes and control characters for database safety."""
    if not val:
        return val
    cleaned = ANSI_ESCAPE_PATTERN.sub("", val)
    cleaned = CONTROL_CHARS_PATTERN.sub("", cleaned)
    return cleaned.strip()


class ExcalidrawParseError(ValueError):
    """Raised when an Excalidraw file or JSON structure is invalid."""


class ExcalidrawParser:
    """Parses Excalidraw JSON schemas into node graphs and connections."""

    @classmethod
    def parse_file(cls, file_path: str | Path) -> ExcalidrawDiagram:
        """Parse a single .excalidraw or .json diagram file."""
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"Excalidraw file not found at '{path}'")

        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = path.read_text(encoding="utf-8-sig")

        diagram_name = path.stem.replace("_", " ").replace("-", " ").title()
        return cls.parse_json(content, diagram_name=diagram_name, file_path=str(path))

    @classmethod
    def parse_json(
        cls,
        json_data: str | dict[str, Any],
        diagram_name: str = "Excalidraw Diagram",
        file_path: str = "inline",
    ) -> ExcalidrawDiagram:
        """Parse Excalidraw raw JSON dictionary or string into ExcalidrawDiagram."""
        if isinstance(json_data, str):
            try:
                data = json.loads(json_data)
            except json.JSONDecodeError as exc:
                raise ExcalidrawParseError(f"Invalid JSON in Excalidraw payload: {exc}") from exc
        else:
            data = json_data

        if "elements" not in data or not isinstance(data.get("elements"), list):
            raise ExcalidrawParseError("Excalidraw payload missing 'elements' list")
        elements_raw = data["elements"]

        raw_elements: list[dict[str, Any]] = [
            e for e in elements_raw if isinstance(e, dict) and not e.get("isDeleted", False)
        ]

        # 1. Map container IDs to attached text labels
        container_to_text: dict[str, str] = {}
        for elem in raw_elements:
            if elem.get("type") == "text":
                c_id = elem.get("containerId")
                text_val = _clean_text(elem.get("text", "")) or ""
                if c_id and text_val:
                    container_to_text[c_id] = text_val

        # 2. Extract shape elements (nodes)
        nodes: list[ExcalidrawElement] = []
        node_lookup: dict[str, ExcalidrawElement] = {}

        for elem in raw_elements:
            e_type = elem.get("type", "unknown")
            e_id = elem.get("id", "")
            if e_type in {"rectangle", "diamond", "ellipse", "frame"}:
                attached_text = container_to_text.get(e_id) or _clean_text(elem.get("text"))
                name_attr = _clean_text(elem.get("name"))
                label = attached_text or name_attr or f"{e_type}-{e_id[:6]}"
                container_id = elem.get("frameId") or elem.get("containerId")

                node_obj = ExcalidrawElement(
                    id=e_id,
                    type=e_type,
                    label=label,
                    text=attached_text,
                    container_id=container_id,
                    x=float(elem.get("x", 0.0)),
                    y=float(elem.get("y", 0.0)),
                    width=float(elem.get("width", 0.0)),
                    height=float(elem.get("height", 0.0)),
                    background_color=elem.get("backgroundColor"),
                    stroke_color=elem.get("strokeColor"),
                )
                nodes.append(node_obj)
                node_lookup[e_id] = node_obj

        # 3. Extract standalone text blocks (annotations, headings)
        standalone_texts: list[str] = []
        for elem in raw_elements:
            if elem.get("type") == "text" and not elem.get("containerId"):
                txt = _clean_text(elem.get("text", ""))
                if txt:
                    standalone_texts.append(txt)

        # 4. Extract relationships / arrows (connections)
        connections: list[ExcalidrawConnection] = []
        for elem in raw_elements:
            if elem.get("type") in {"arrow", "line"}:
                start_b = elem.get("startBinding") or {}
                end_b = elem.get("endBinding") or {}
                src_id = start_b.get("elementId") if isinstance(start_b, dict) else None
                tgt_id = end_b.get("elementId") if isinstance(end_b, dict) else None

                if src_id or tgt_id:
                    resolved_src_id = src_id or "unknown"
                    resolved_tgt_id = tgt_id or "unknown"

                    src_node = node_lookup.get(resolved_src_id)
                    tgt_node = node_lookup.get(resolved_tgt_id)

                    src_label = src_node.label if src_node else f"Node-{resolved_src_id[:6]}"
                    tgt_label = tgt_node.label if tgt_node else f"Node-{resolved_tgt_id[:6]}"

                    edge_label = container_to_text.get(elem.get("id", "")) or _clean_text(
                        elem.get("text")
                    )
                    connections.append(
                        ExcalidrawConnection(
                            arrow_id=elem.get("id", ""),
                            source_id=resolved_src_id,
                            target_id=resolved_tgt_id,
                            source_label=src_label,
                            target_label=tgt_label,
                            edge_label=edge_label,
                        )
                    )

        return ExcalidrawDiagram(
            diagram_name=diagram_name,
            file_path=file_path,
            nodes=nodes,
            connections=connections,
            standalone_texts=standalone_texts,
            version=data.get("version", 2),
        )

    @classmethod
    def parse_directory(
        cls,
        dir_path: str | Path,
        recursive: bool = True,
    ) -> list[ExcalidrawDiagram]:
        """Discover and parse all .excalidraw and matching JSON files in a directory."""
        directory = Path(dir_path)
        if not directory.is_dir():
            raise NotADirectoryError(f"Directory not found: '{directory}'")

        patterns = ["*.excalidraw", "*.json"]
        files: list[Path] = []
        for pat in patterns:
            if recursive:
                files.extend(directory.rglob(pat))
            else:
                files.extend(directory.glob(pat))

        diagrams: list[ExcalidrawDiagram] = []
        for f in sorted(dict.fromkeys(files)):
            try:
                diag = cls.parse_file(f)
                # Only keep files that actually contain Excalidraw elements
                if diag.nodes or diag.connections or diag.standalone_texts:
                    diagrams.append(diag)
            except Exception:
                continue

        return diagrams
