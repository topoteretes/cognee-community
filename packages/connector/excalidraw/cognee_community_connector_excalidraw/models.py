"""Data models for Excalidraw visual diagrams and topology elements."""

from dataclasses import dataclass, field


@dataclass
class ExcalidrawElement:
    """Represents a shape, text block, or visual element in an Excalidraw diagram."""

    id: str
    type: str  # rectangle, diamond, ellipse, text, arrow, line, frame
    text: str | None = None
    container_id: str | None = None
    start_binding: str | None = None
    end_binding: str | None = None
    label: str | None = None
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0
    background_color: str | None = None
    stroke_color: str | None = None


@dataclass
class ExcalidrawConnection:
    """Represents an architectural relationship / edge connecting two diagram elements."""

    arrow_id: str
    source_id: str
    target_id: str
    source_label: str
    target_label: str
    edge_label: str | None = None


@dataclass
class ExcalidrawDiagram:
    """Complete parsed Excalidraw diagram entity ready for Cognee NLP ingestion."""

    diagram_name: str
    file_path: str
    nodes: list[ExcalidrawElement] = field(default_factory=list)
    connections: list[ExcalidrawConnection] = field(default_factory=list)
    standalone_texts: list[str] = field(default_factory=list)
    version: int = 2


@dataclass
class RenderOptions:
    """Rendering configuration for formatting diagram documents."""

    include_mermaid: bool = True
    include_tables: bool = True
    include_isolated_nodes: bool = True
