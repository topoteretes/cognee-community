"""Export the TurboPuffer graph (nodes + edges) to JSON files for inspection.

Writes nodes.json and edges.json with each record's filterable attributes plus
the full `properties` blob expanded back into nested JSON (not a string), so the
payload is human-readable and diffable against other graph backends.

Usage:
    # uses the default single-tenant prefix written by example.py
    python examples/export_graph.py

    # custom dataset prefix / output dir / row cap
    python examples/export_graph.py --db cognee_graph --out ./turbopuffer_graph_dump --limit 5000

Reads TURBOPUFFER_API_KEY and TURBOPUFFER_REGION from the environment.
"""

import argparse
import json
import os

import turbopuffer


def _export(client, namespace_name: str, out_path: str, limit: int) -> int:
    rows = (
        client.namespace(namespace_name)
        .query(rank_by=("id", "asc"), top_k=limit, include_attributes=True)
        .rows
        or []
    )
    records = []
    for row in rows:
        record = dict(row.model_extra or {})
        record["id"] = str(row.id)
        if isinstance(record.get("properties"), str):
            try:
                record["properties"] = json.loads(record["properties"])
            except (TypeError, ValueError):
                pass
        records.append(record)
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(records, handle, indent=2, ensure_ascii=False)
    return len(records)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="cognee_graph", help="dataset namespace prefix")
    parser.add_argument("--out", default="turbopuffer_graph_dump", help="output directory")
    parser.add_argument("--limit", type=int, default=10000, help="max rows per namespace")
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)
    client = turbopuffer.Turbopuffer(
        api_key=os.environ["TURBOPUFFER_API_KEY"],
        region=os.getenv("TURBOPUFFER_REGION", "gcp-us-central1"),
    )

    for collection, fname in (("graph_node", "nodes.json"), ("graph_edge", "edges.json")):
        ns = f"{args.db}_{collection}"
        path = os.path.join(args.out, fname)
        try:
            n = _export(client, ns, path, args.limit)
            print(f"wrote {n} records from {ns} -> {path}")
        except Exception as error:
            print(f"skip {ns}: {error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
