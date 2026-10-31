"""Read-only synthetic-project access check. Prints no definition bodies or secrets."""

import argparse
import json
import os
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

from cognee_community_connector_mixpanel import mixpanel_source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-id", type=int, action="append", default=None)
    parser.add_argument("--cohort-id", type=int, action="append", default=None)
    parser.add_argument("--schema-name", action="append", default=None)
    args = parser.parse_args()
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    workspace = os.getenv("MIXPANEL_WORKSPACE_ID")
    source = mixpanel_source(
        workspace_id=int(workspace) if workspace else None,
        report_ids=args.report_id,
        cohort_ids=args.cohort_id,
        schema_names=args.schema_name,
    )
    rows = list(source)
    counts = Counter()
    for row in rows:
        counts[json.loads(row["id"])[1]] += 1
    print("Configured project/workspace presence and selected definitions verified.")
    for kind in ("schema", "report", "cohort"):
        print(f"{kind}: {counts[kind]} selected definitions parsed")
    print("No staging or ingestion performed; no credentials or definition bodies printed.")
    print("This does not establish inventory completeness or sharing/deletion semantics.")


if __name__ == "__main__":
    main()
