"""Example demonstration of Cognee Excalidraw visual diagram connector."""

import asyncio
import json
from pathlib import Path

from cognee_community_connector_excalidraw import (
    ExcalidrawConnector,
    ExcalidrawParser,
    render_diagram_markdown,
)

SAMPLE_DIAGRAM = {
    "type": "excalidraw",
    "version": 2,
    "elements": [
        {
            "id": "fe",
            "type": "rectangle",
            "width": 140,
            "height": 70,
        },
        {
            "id": "fe_txt",
            "type": "text",
            "text": "React Frontend",
            "containerId": "fe",
        },
        {
            "id": "be",
            "type": "rectangle",
            "width": 140,
            "height": 70,
        },
        {
            "id": "be_txt",
            "type": "text",
            "text": "FastAPI Core",
            "containerId": "be",
        },
        {
            "id": "db",
            "type": "ellipse",
            "width": 120,
            "height": 70,
        },
        {
            "id": "db_txt",
            "type": "text",
            "text": "Qdrant Vector DB",
            "containerId": "db",
        },
        {
            "id": "arr1",
            "type": "arrow",
            "startBinding": {"elementId": "fe"},
            "endBinding": {"elementId": "be"},
        },
        {
            "id": "arr2",
            "type": "arrow",
            "startBinding": {"elementId": "be"},
            "endBinding": {"elementId": "db"},
        },
        {
            "id": "note",
            "type": "text",
            "text": "Note: Vector search uses cosine similarity with 1536d embeddings.",
        },
    ],
}


async def main() -> None:
    print("=== Cognee Excalidraw Connector Demo ===\n")

    # 1. Parsing raw Excalidraw JSON
    print("1. Parsing Excalidraw architecture diagram...")
    diag = ExcalidrawParser.parse_json(SAMPLE_DIAGRAM, diagram_name="AI Agent Architecture")
    print(f"Diagram Name:  {diag.diagram_name}")
    labels_str = ", ".join(n.label or "" for n in diag.nodes)
    print(f"Components:    {len(diag.nodes)} nodes ({labels_str})")
    print(f"Relationships: {len(diag.connections)} directional connections")
    print(f"Annotations:   {len(diag.standalone_texts)} notes\n")

    # 2. Rendering into Cognee Markdown & Mermaid flowchart
    print("2. Generated Markdown Representation (Preview):")
    markdown = render_diagram_markdown(diag)
    print(markdown[:380] + "...\n")

    # 3. Reading from disk
    demo_file = Path(__file__).parent / "demo_architecture.excalidraw"
    if not demo_file.exists():
        demo_file.write_text(json.dumps(SAMPLE_DIAGRAM, indent=2), encoding="utf-8")
    print(f"3. Saved demo file to {demo_file.name}")

    # 4. Ingestion into Cognee memory
    print("4. Preparing Cognee document for knowledge graph ingestion...")
    connector = ExcalidrawConnector()
    docs = await connector.fetch_documents(file_paths=[demo_file])
    for doc in docs:
        hash_prefix = doc["data_id"][:12]
        print(f"Generated Cognee Document: [{doc['id']}] '{doc['title']}' ({hash_prefix}...)")

    print("\nNext step: Pass doc to `cognee.add(doc['content'])` and `await cognee.cognify()`")


if __name__ == "__main__":
    asyncio.run(main())
