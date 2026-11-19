"""Unit tests for Cognee Excalidraw visual diagram connector."""

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from cognee_community_connector_excalidraw.cli import main as cli_main
from cognee_community_connector_excalidraw.excalidraw import (
    DOCUMENT_SOURCE_ATTR,
    ExcalidrawConnector,
    excalidraw_source,
)
from cognee_community_connector_excalidraw.formatter import (
    diagram_to_cognee_document,
    render_diagram_markdown,
)
from cognee_community_connector_excalidraw.models import (
    ExcalidrawConnection,
    ExcalidrawDiagram,
    ExcalidrawElement,
    RenderOptions,
)
from cognee_community_connector_excalidraw.parser import (
    ExcalidrawParseError,
    ExcalidrawParser,
)

SAMPLE_EXCALIDRAW_DATA = {
    "type": "excalidraw",
    "version": 2,
    "source": "https://excalidraw.com",
    "elements": [
        {
            "id": "node_1",
            "type": "rectangle",
            "x": 100,
            "y": 100,
            "width": 150,
            "height": 80,
            "backgroundColor": "#e7f5ff",
            "strokeColor": "#1c7ed6",
        },
        {
            "id": "text_1",
            "type": "text",
            "x": 120,
            "y": 130,
            "text": "Auth Service",
            "containerId": "node_1",
        },
        {
            "id": "node_2",
            "type": "rectangle",
            "x": 350,
            "y": 100,
            "width": 150,
            "height": 80,
        },
        {
            "id": "text_2",
            "type": "text",
            "x": 370,
            "y": 130,
            "text": "PostgreSQL DB",
            "containerId": "node_2",
        },
        {
            "id": "arrow_1",
            "type": "arrow",
            "startBinding": {"elementId": "node_1"},
            "endBinding": {"elementId": "node_2"},
        },
        {
            "id": "text_arrow",
            "type": "text",
            "text": "SQL query",
            "containerId": "arrow_1",
        },
        {
            "id": "standalone_note",
            "type": "text",
            "x": 50,
            "y": 300,
            "text": "Architecture RFC: Auth system uses JWT and connection pooling.",
        },
        {
            "id": "deleted_element",
            "type": "rectangle",
            "isDeleted": True,
        },
    ],
}


def test_parse_json_valid_diagram():
    diag = ExcalidrawParser.parse_json(SAMPLE_EXCALIDRAW_DATA, diagram_name="Auth Architecture")
    assert diag.diagram_name == "Auth Architecture"
    assert len(diag.nodes) == 2
    assert diag.nodes[0].label == "Auth Service"
    assert diag.nodes[1].label == "PostgreSQL DB"
    assert len(diag.connections) == 1
    assert diag.connections[0].source_label == "Auth Service"
    assert diag.connections[0].target_label == "PostgreSQL DB"
    assert diag.connections[0].edge_label == "SQL query"
    assert len(diag.standalone_texts) == 1
    assert "Auth system uses JWT" in diag.standalone_texts[0]


def test_parse_json_missing_elements_raises():
    with pytest.raises(ExcalidrawParseError, match="missing 'elements'"):
        ExcalidrawParser.parse_json({"type": "excalidraw"})


def test_parse_json_invalid_syntax_raises():
    with pytest.raises(ExcalidrawParseError, match="Invalid JSON"):
        ExcalidrawParser.parse_json("{invalid_json_payload...")


def test_parse_file_from_disk():
    with tempfile.NamedTemporaryFile("w+", suffix=".excalidraw", delete=False) as f:
        json.dump(SAMPLE_EXCALIDRAW_DATA, f)
        temp_path = f.name

    try:
        diag = ExcalidrawParser.parse_file(temp_path)
        assert len(diag.nodes) == 2
        assert len(diag.connections) == 1
    finally:
        Path(temp_path).unlink(missing_ok=True)


def test_parse_file_not_found_raises():
    with pytest.raises(FileNotFoundError):
        ExcalidrawParser.parse_file("non_existent_diagram.excalidraw")


def test_parse_directory_discovers_diagrams():
    with tempfile.TemporaryDirectory() as tmpdir:
        path1 = Path(tmpdir) / "arch.excalidraw"
        path2 = Path(tmpdir) / "workflow.json"
        path3 = Path(tmpdir) / "ignored.txt"

        path1.write_text(json.dumps(SAMPLE_EXCALIDRAW_DATA), encoding="utf-8")
        path2.write_text(json.dumps(SAMPLE_EXCALIDRAW_DATA), encoding="utf-8")
        path3.write_text("not a diagram", encoding="utf-8")

        diagrams = ExcalidrawParser.parse_directory(tmpdir)
        assert len(diagrams) == 2


def test_parse_directory_missing_raises():
    with pytest.raises(NotADirectoryError):
        ExcalidrawParser.parse_directory("non_existent_folder_xyz")


def test_render_diagram_markdown():
    nodes = [
        ExcalidrawElement(id="n1", type="rectangle", label="Client Web App"),
        ExcalidrawElement(id="n2", type="rectangle", label="Backend API"),
    ]
    conns = [
        ExcalidrawConnection(
            arrow_id="a1",
            source_id="n1",
            target_id="n2",
            source_label="Client Web App",
            target_label="Backend API",
            edge_label="HTTPS REST",
        )
    ]
    diag = ExcalidrawDiagram(
        diagram_name="Payment Flow",
        file_path="diagram.excalidraw",
        nodes=nodes,
        connections=conns,
        standalone_texts=["Notes on architecture"],
    )

    md = render_diagram_markdown(diag)
    assert "# Diagram: Payment Flow" in md
    assert "```mermaid" in md
    assert "flowchart TD" in md
    assert "Client Web App" in md
    assert "Backend API" in md
    assert "HTTPS REST" in md
    assert "## Architectural Relationships" in md
    assert "| `Client Web App` | `HTTPS REST` | `Backend API` |" in md
    assert "## Annotations & Notes" in md


def test_render_diagram_respects_options():
    nodes = [ExcalidrawElement(id="n1", type="rectangle", label="Service")]
    diag = ExcalidrawDiagram(diagram_name="Tiny Diagram", file_path="tiny.json", nodes=nodes)

    opts = RenderOptions(include_mermaid=False, include_tables=False)
    md = render_diagram_markdown(diag, options=opts)
    assert "```mermaid" not in md
    assert "## Component Inventory" not in md


def test_diagram_to_cognee_document():
    diag = ExcalidrawParser.parse_json(SAMPLE_EXCALIDRAW_DATA, diagram_name="System Graph")
    doc = diagram_to_cognee_document(diag)

    assert doc["id"] == "excalidraw:system_graph"
    assert doc["title"] == "Diagram: System Graph"
    assert doc["source"] == "excalidraw"
    assert doc["nodes_count"] == 2
    assert doc["connections_count"] == 1
    assert len(doc["data_id"]) == 64
    assert doc["_deleted"] is False


def test_excalidraw_source_generator():
    docs = list(excalidraw_source(json_payloads=[SAMPLE_EXCALIDRAW_DATA]))
    assert len(docs) == 1
    assert docs[0]["id"] == "excalidraw:inline_diagram_1"
    assert docs[0][DOCUMENT_SOURCE_ATTR] is True


def test_excalidraw_connector_fetch_documents():
    async def _run():
        connector = ExcalidrawConnector()
        docs = await connector.fetch_documents(json_payloads=[SAMPLE_EXCALIDRAW_DATA])
        assert len(docs) == 1
        assert connector.sync_stats["processed"] == 1
        assert connector.sync_stats["failed"] == 0

    asyncio.run(_run())


def test_excalidraw_connector_repr():
    connector = ExcalidrawConnector()
    assert "ExcalidrawConnector" in repr(connector)


def test_cli_version_command(capsys):
    ret = cli_main(["version"])
    assert ret == 0
    captured = capsys.readouterr()
    assert "0.1.0" in captured.out


def test_cli_benchmark_command(capsys):
    ret = cli_main(["benchmark"])
    assert ret == 0
    captured = capsys.readouterr()
    assert "Cognee Excalidraw Microbenchmark" in captured.out
    assert "Throughput:" in captured.out


def test_cli_scan_command(capsys):
    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = Path(tmpdir) / "test.excalidraw"
        fpath.write_text(json.dumps(SAMPLE_EXCALIDRAW_DATA), encoding="utf-8")

        ret = cli_main(["scan", tmpdir])
        assert ret == 0
        captured = capsys.readouterr()
        assert "Discovered 1 Excalidraw diagram" in captured.out


def test_cli_parse_command(capsys):
    with tempfile.NamedTemporaryFile("w+", suffix=".excalidraw", delete=False) as f:
        json.dump(SAMPLE_EXCALIDRAW_DATA, f)
        temp_path = f.name

    try:
        ret = cli_main(["parse", temp_path, "--json"])
        assert ret == 0
        captured = capsys.readouterr()
        doc = json.loads(captured.out)
        assert doc["source"] == "excalidraw"
    finally:
        Path(temp_path).unlink(missing_ok=True)


def test_ansi_and_control_sanitization():
    dirty_text = "Microservice\x1b[31;1m Alert\x00\x07"
    raw_data = {
        "type": "excalidraw",
        "version": 2,
        "source": "https://excalidraw.com",
        "elements": [
            {
                "id": "box_ansi",
                "type": "rectangle",
                "isDeleted": False,
                "text": dirty_text,
            }
        ],
    }
    diag = ExcalidrawParser.parse_json(raw_data, diagram_name="ANSI Cleaned")
    assert "\x1b" not in diag.nodes[0].label
    assert "\x00" not in diag.nodes[0].label
    assert "Alert" in diag.nodes[0].label


def test_parse_dangling_arrow_without_bindings():
    raw_data = {
        "type": "excalidraw",
        "version": 2,
        "elements": [
            {
                "id": "arrow_free",
                "type": "arrow",
                "isDeleted": False,
                "startBinding": None,
                "endBinding": {"elementId": "non_existent_node"},
                "text": "floating pointer",
            }
        ],
    }
    diag = ExcalidrawParser.parse_json(raw_data, diagram_name="Dangling Arrow")
    assert len(diag.connections) == 1
    assert diag.connections[0].source_id == "unknown"
    assert diag.connections[0].target_id == "non_existent_node"


def test_parse_container_grouped_elements():
    raw_data = {
        "type": "excalidraw",
        "version": 2,
        "elements": [
            {
                "id": "frame1",
                "type": "frame",
                "isDeleted": False,
                "name": "VPC Network",
            },
            {
                "id": "worker1",
                "type": "rectangle",
                "isDeleted": False,
                "frameId": "frame1",
                "text": "Worker 1",
            },
        ],
    }
    diag = ExcalidrawParser.parse_json(raw_data, diagram_name="Grouped Elements")
    assert len(diag.nodes) == 2
    worker = next(n for n in diag.nodes if n.id == "worker1")
    assert worker.container_id == "frame1"


def test_excalidraw_connector_empty_input():
    async def _run():
        connector = ExcalidrawConnector()
        docs = await connector.fetch_documents()
        assert len(docs) == 0
        assert connector.sync_stats["processed"] == 0
        assert connector.sync_stats["failed"] == 0

    asyncio.run(_run())
