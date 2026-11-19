"""CLI interface for the Cognee Excalidraw visual diagram connector."""

import argparse
import json
import sys
import time
import tracemalloc
from pathlib import Path

from cognee_community_connector_excalidraw.formatter import (
    diagram_to_cognee_document,
    render_diagram_markdown,
)
from cognee_community_connector_excalidraw.models import RenderOptions
from cognee_community_connector_excalidraw.parser import ExcalidrawParser


def cmd_parse(args: argparse.Namespace) -> int:
    """Parse an Excalidraw file and render Markdown or Cognee JSON."""
    try:
        diag = ExcalidrawParser.parse_file(args.file_path)
    except Exception as exc:
        print(f"Error parsing '{args.file_path}': {exc}", file=sys.stderr)
        return 1

    options = RenderOptions(include_mermaid=not args.no_mermaid)

    if args.json:
        doc = diagram_to_cognee_document(diag, options=options)
        print(json.dumps(doc, indent=2))
    else:
        print(render_diagram_markdown(diag, options=options))
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    """Scan a folder for Excalidraw diagrams and print statistics."""
    try:
        diagrams = ExcalidrawParser.parse_directory(args.directory_path)
    except Exception as exc:
        print(f"Error scanning '{args.directory_path}': {exc}", file=sys.stderr)
        return 1

    print(f"Discovered {len(diagrams)} Excalidraw diagram(s) in {args.directory_path}:")
    for d in diagrams:
        print(
            f"  - [{d.diagram_name}] "
            f"{len(d.nodes)} nodes, {len(d.connections)} edges, "
            f"{len(d.standalone_texts)} notes ({Path(d.file_path).name})"
        )
    return 0


def cmd_benchmark(args: argparse.Namespace) -> int:
    """Run an offline microbenchmark measuring parsing and Markdown rendering."""
    sample_payload = {
        "type": "excalidraw",
        "version": 2,
        "elements": [
            {
                "id": "srv_1",
                "type": "rectangle",
                "x": 100,
                "y": 100,
                "width": 160,
                "height": 90,
            },
            {
                "id": "txt_1",
                "type": "text",
                "x": 120,
                "y": 130,
                "text": "Payment API Gateway",
                "containerId": "srv_1",
            },
            {
                "id": "db_1",
                "type": "rectangle",
                "x": 400,
                "y": 100,
                "width": 160,
                "height": 90,
            },
            {
                "id": "txt_2",
                "type": "text",
                "x": 420,
                "y": 130,
                "text": "Stripe Ledger DB",
                "containerId": "db_1",
            },
            {
                "id": "arr_1",
                "type": "arrow",
                "startBinding": {"elementId": "srv_1"},
                "endBinding": {"elementId": "db_1"},
            },
            {
                "id": "txt_arr",
                "type": "text",
                "text": "gRPC SSL",
                "containerId": "arr_1",
            },
            {
                "id": "note_1",
                "type": "text",
                "x": 50,
                "y": 300,
                "text": "Architecture RFC 42: zero-trust network perimeter",
            },
        ],
    }

    raw_json = json.dumps(sample_payload)

    iterations = 2000
    tracemalloc.start()
    t0 = time.perf_counter()

    for _ in range(iterations):
        diag = ExcalidrawParser.parse_json(raw_json, diagram_name="Benchmark Architecture")
        _ = diagram_to_cognee_document(diag)

    elapsed_ms = (time.perf_counter() - t0) * 1000
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    per_doc_ms = elapsed_ms / iterations
    docs_per_sec = (iterations / elapsed_ms) * 1000
    peak_kb = peak_bytes / 1024

    print("=== Cognee Excalidraw Microbenchmark ===")
    print(f"Iterations:          {iterations}")
    print(f"Total time:          {elapsed_ms:.2f} ms")
    print(f"Per-document time:   {per_doc_ms:.4f} ms")
    print(f"Throughput:          {docs_per_sec:,.0f} diagrams/sec")
    print(f"Peak memory delta:   {peak_kb:.2f} KB")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for cognee-excalidraw."""
    parser = argparse.ArgumentParser(
        prog="cognee-excalidraw",
        description="Cognee Excalidraw visual architecture diagram connector CLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # parse
    p_parse = subparsers.add_parser("parse", help="Parse and render an Excalidraw file")
    p_parse.add_argument("file_path", help="Path to .excalidraw or JSON file")
    p_parse.add_argument("--no-mermaid", action="store_true", help="Omit Mermaid flowchart")
    p_parse.add_argument("--json", action="store_true", help="Output Cognee JSON document")
    p_parse.set_defaults(func=cmd_parse)

    # scan
    p_scan = subparsers.add_parser("scan", help="Scan directory for Excalidraw files")
    p_scan.add_argument("directory_path", help="Directory path to scan")
    p_scan.set_defaults(func=cmd_scan)

    # benchmark
    p_bench = subparsers.add_parser("benchmark", help="Run local memory and latency benchmark")
    p_bench.set_defaults(func=cmd_benchmark)

    # version
    p_ver = subparsers.add_parser("version", help="Show connector version")
    p_ver.set_defaults(func=lambda _: print("cognee-community-connector-excalidraw 0.1.0") or 0)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
