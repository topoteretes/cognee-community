"""Run a bounded read-only OpenSiro Index -> local Cognee native graph import.

Usage: python examples/example.py --repo ./index --commit <40-hex> \
       --store-root ./local-store --dataset demo --ids goose,langflow --lexical
"""

import argparse
import asyncio
import json
import os
from pathlib import Path

from cognee_community_connector_opensiro_vsm.source import read_snapshot


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--repo", type=Path, required=True)
    cli.add_argument("--commit", required=True)
    cli.add_argument("--store-root", type=Path, required=True)
    cli.add_argument("--dataset", required=True)
    selection = cli.add_mutually_exclusive_group(required=True)
    selection.add_argument("--ids", help="Comma-separated accepted harness IDs")
    selection.add_argument("--all", action="store_true", help="Import every accepted assessment")
    cli.add_argument("--lexical", action="store_true")
    args = cli.parse_args()
    snap = read_snapshot(args.repo, args.commit)  # source contract before any SDK calls
    selected = (
        tuple(sorted(snap.accepted))
        if args.all
        else tuple(x.strip() for x in args.ids.split(",") if x.strip())
    )
    if not selected or not all(x in snap.accepted for x in selected):
        cli.error("all --ids must be accepted in the pinned Index snapshot")
    root = args.store_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    os.environ.update(
        SYSTEM_ROOT_DIRECTORY=str(root / "system"),
        DATA_ROOT_DIRECTORY=str(root / "data"),
        DLT_DATA_DIR=str(root / "dlt"),
        PIPELINES_DIR=str(root / "dlt" / "pipelines"),
        COGNEE_SKIP_CONNECTION_TEST="true",
        TELEMETRY_DISABLED="1",
    )
    # Configure Cognee's stores BEFORE importing its SDK.
    from cognee_community_connector_opensiro_vsm.native import ingest_fresh

    result = asyncio.run(
        ingest_fresh(
            snapshot=snap,
            selected=selected,
            dataset=args.dataset,
            tag="opensiro_vsm_canonical_example",
            lexical=args.lexical,
            attempt_root=root / "stage-attempts",
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
