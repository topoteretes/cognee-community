"""Read-only OpenSiro source CLI, with no Cognee writes.

Examples:
    python -m cognee_community_connector_opensiro_vsm validate --repo ./index --commit SHA40
    python -m cognee_community_connector_opensiro_vsm diff --repo ./index \
        --old OLD_SHA40 --new NEW_SHA40
"""

import argparse
import json
from pathlib import Path

from .source import SourceError, diff_snapshots, inspect_one_historical_record, read_snapshot


def _records_manifest(snapshot):
    recs = sorted(snapshot.accepted.values(), key=lambda x: x.harness_id)
    return {
        "schema": "opensiro-vsm-source-prototype/v0",
        "index_commit": snapshot.commit,
        "admission_scope": "included_only",
        "count": len(recs),
        "assessments": [
            {
                "external_id": rec.external_id,
                "version_id": rec.version_id,
                "harness_id": rec.harness_id,
                "title": rec.project_name,
                "repository": rec.repository,
                "review_ref": rec.review_ref,
                "profile_version": rec.profile_version,
                "assessment_procedure_version": rec.assessment_procedure_version,
                "source_url": rec.assessment_url,
                "source_sha256": rec.sha256,
                "states": rec.grades(),
                "primary_evidence": list(rec.primary_evidence),
                # Full source markdown is referenced and may be read from pinned Git.
                # The manifest does not rewrite/normalize the authoritative bytes.
            }
            for rec in recs
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(prog="cognee_community_connector_opensiro_vsm")
    commands = parser.add_subparsers(dest="action", required=True)
    v = commands.add_parser("validate", help="Validate one pinned canonical Git tree.")
    v.add_argument("--repo", required=True)
    v.add_argument("--commit", required=True)
    d = commands.add_parser("diff", help="Plan old vs new pinned admission state without writing.")
    d.add_argument("--repo", required=True)
    d.add_argument("--old", required=True)
    d.add_argument("--new", required=True)
    h = commands.add_parser(
        "diff-one",
        help="Historical single-record inspection only, NOT full snapshot admission validation",
    )
    h.add_argument("--repo", required=True)
    h.add_argument("--old", required=True)
    h.add_argument("--new", required=True)
    h.add_argument("--harness", required=True)
    m = commands.add_parser(
        "manifest", help="Emit pinned admitted source manifest, not Cognee data."
    )
    m.add_argument("--repo", required=True)
    m.add_argument("--commit", required=True)
    m.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "diff":
            old = read_snapshot(args.repo, args.old)
            new = read_snapshot(args.repo, args.new)
            result = {"status": "PASS_SOURCE_DIFF", **diff_snapshots(old, new)}
        elif args.action == "diff-one":
            old = inspect_one_historical_record(args.repo, args.old, args.harness)
            new = inspect_one_historical_record(args.repo, args.new, args.harness)
            result = {
                "status": "PASS_SCOPED_HISTORY_ONLY",
                "note": "No full snapshot canonical admission claim",
                "harness": args.harness,
                "old_status": old.status,
                "new_status": new.status,
                "same_review_ref": old.review_ref == new.review_ref,
                "same_content_sha256": old.sha256 == new.sha256,
                "changed_grades": {
                    f: {"old": old.grades()[f], "new": new.grades()[f]}
                    for f in old.grades()
                    if old.grades()[f] != new.grades()[f]
                },
            }
        else:
            src = read_snapshot(args.repo, args.commit)
            result = {"status": "PASS_SOURCE_VALIDATION", **src.stats()}
            if args.action == "manifest":
                manifest = _records_manifest(src)
                path = Path(args.output)
                if not path.parent.exists():
                    raise SourceError(f"Output parent does not exist: {path.parent}")
                # No atomic snapshot swap or DLT modification; this is only a local
                # manifest write. Avoid accidental overwriting by exclusive creation.
                with path.open("x", encoding="utf8") as handle:
                    json.dump(manifest, handle, indent=2, ensure_ascii=False)
                    handle.write("\n")
                result["manifest_output"] = str(path)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (SourceError, FileExistsError) as exc:
        print(json.dumps({"status": "FAIL_CLOSED", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
