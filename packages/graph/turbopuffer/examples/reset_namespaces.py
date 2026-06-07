"""Delete TurboPuffer namespaces so the example can rebuild from a clean slate.

By default deletes ONLY cognee-generated namespaces (graph + vector + this repo's
test/debug prefixes) in the configured region. Pass --all to delete every
namespace in the region instead.

Usage:
    python examples/reset_namespaces.py            # cognee/test namespaces only
    python examples/reset_namespaces.py --all       # everything in the region
    python examples/reset_namespaces.py --dry-run    # list, delete nothing

Reads TURBOPUFFER_API_KEY and TURBOPUFFER_REGION from the environment.
"""

import os
import sys

import turbopuffer

# Namespace name fragments that identify cognee-generated data.
_GRAPH_SUFFIXES = ("_graph_node", "_graph_edge")
# cognee vector collections are "{db}_{Model}_{field}"; these are the field tails.
_VECTOR_SUFFIXES = (
    "_text",
    "_name",
    "_relationship_name",
)
# Prefixes used by this package's tests / debugging sessions.
_TEST_PREFIXES = ("tpuf_graph_test_", "tpuf_iso_", "tpuf_bigstr_", "dbg_", "edgedbg_")


def _is_cognee_namespace(name: str) -> bool:
    if name.endswith(_GRAPH_SUFFIXES):
        return True
    if name.endswith(_VECTOR_SUFFIXES):
        return True
    if name.startswith(_TEST_PREFIXES):
        return True
    return False


def main() -> int:
    delete_all = "--all" in sys.argv
    dry_run = "--dry-run" in sys.argv

    region = os.getenv("TURBOPUFFER_REGION", "gcp-us-central1")
    client = turbopuffer.Turbopuffer(
        api_key=os.environ["TURBOPUFFER_API_KEY"],
        region=region,
    )

    names = []
    for ns in client.namespaces():
        names.append(getattr(ns, "id", None) or str(ns))

    targets = names if delete_all else [n for n in names if _is_cognee_namespace(n)]
    skipped = [n for n in names if n not in targets]

    print(f"region={region}  total namespaces={len(names)}  to delete={len(targets)}")
    if skipped and not delete_all:
        print(f"skipping {len(skipped)} non-cognee namespaces: {skipped}")

    for name in targets:
        if dry_run:
            print(f"  [dry-run] would delete {name}")
            continue
        try:
            client.namespace(name).delete_all()
            print(f"  deleted {name}")
        except Exception as error:
            msg = str(error).lower()
            if "404" in msg or "not found" in msg:
                print(f"  already gone {name}")
            else:
                print(f"  ERROR deleting {name}: {error}")

    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
