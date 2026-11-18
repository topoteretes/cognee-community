"""Read-only developer path: native Cognee lexical search -> pinned source inspection.

Run AFTER examples/example.py --lexical. Never imports or mutates a dataset.
Choose --inspect from the printed native search hits; this command does not
invent source links or treat lexical rank as a quality/performance metric.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from cognee_community_connector_opensiro_vsm.source import SourceError, read_snapshot

SOURCE_PREFIX = "opensiro/vsm-harness-index:"
SOURCE_TAG = "opensiro_vsm_canonical_example"


def result_hits(response: object, snapshot: object) -> list[dict]:
    """Interpret native DocumentChunk responses without guessing IDs from prose."""
    results = []
    for group in response:
        chunk_list = (
            group.get("search_result")
            if isinstance(group, dict)
            else getattr(group, "search_result", None)
        )
        if not isinstance(chunk_list, (list, tuple)):
            raise SourceError("Unexpected native lexical response; inspect Cognee SDK contract")
        for chunk in chunk_list:
            kind = chunk.get("type") if isinstance(chunk, dict) else getattr(chunk, "type", None)
            external_id = (
                chunk.get("document_id")
                if isinstance(chunk, dict)
                else getattr(chunk, "document_id", None)
            )
            content = chunk.get("text") if isinstance(chunk, dict) else getattr(chunk, "text", None)
            if (
                kind != "DocumentChunk"
                or not isinstance(external_id, str)
                or not external_id.startswith(SOURCE_PREFIX)
            ):
                raise SourceError("Native result lacks a canonical OpenSiro DocumentChunk identity")
            identifier = external_id[len(SOURCE_PREFIX) :]
            if identifier not in snapshot.accepted or not isinstance(content, str):
                raise SourceError("Native result is not in the pinned admitted source snapshot")
            record = snapshot.accepted[identifier]
            section = next((line for line in content.splitlines() if line.startswith("## S")), "")
            results.append(
                {
                    "rank": len(results) + 1,
                    "harness_id": identifier,
                    "project": record.project_name,
                    "section": section,
                    "excerpt": content[:320],
                    "assessment_url": record.assessment_url,
                }
            )
    return results


def source_detail(
    snapshot: object, identifier: str, returned: list[dict], show_markdown: bool
) -> dict:
    """Allow inspection only of a real returned hit in the pinned snapshot."""
    if identifier not in {item["harness_id"] for item in returned}:
        raise SourceError("Inspection ID must be present among this query's native hits")
    record = snapshot.accepted[identifier]
    detail = {
        "harness_id": record.harness_id,
        "project": record.project_name,
        "canonical_assessment": record.assessment_url,
        "assessment_sha256": record.sha256,
        "repository": record.repository,
        "reviewed_upstream_commit": record.review_ref,
        "pinned_upstream_tree": record.repository.rstrip("/") + "/tree/" + record.review_ref,
        "autonomy_labels_not_performance_scores": record.grades(),
        "primary_evidence_bullets": list(record.primary_evidence),
    }
    if show_markdown:
        detail["full_canonical_markdown"] = record.raw_bytes.decode("utf-8")
    return detail


def configure_existing_store(root: Path) -> None:
    if not root.is_dir():
        raise SourceError("Existing --store-root required; run example.py import first")
    os.environ.update(
        SYSTEM_ROOT_DIRECTORY=str(root / "system"),
        DATA_ROOT_DIRECTORY=str(root / "data"),
        DLT_DATA_DIR=str(root / "dlt"),
        PIPELINES_DIR=str(root / "dlt" / "pipelines"),
        COGNEE_SKIP_CONNECTION_TEST="true",
        TELEMETRY_DISABLED="1",
    )


async def search_existing(snapshot: object, dataset: str, query: str, top_k: int) -> list[dict]:
    import cognee
    from cognee.modules.search.types import SearchType

    from cognee_community_connector_opensiro_vsm.audit import audit_dataset
    from cognee_community_connector_opensiro_vsm.native import source_data

    _, _, data_rows, docs = await source_data(dataset, SOURCE_TAG, existing_only=True)
    if not docs or len(docs) != len(data_rows):
        raise SourceError("Existing dataset is empty or contains unexpected source rows")
    selected = tuple(
        sorted(key[len(SOURCE_PREFIX) :] for key in docs if key.startswith(SOURCE_PREFIX))
    )
    if len(selected) != len(docs) or any(x not in snapshot.accepted for x in selected):
        raise SourceError("Existing dataset source identities differ from pinned Index")
    audit = await audit_dataset(snapshot, selected, dataset, SOURCE_TAG, lexical=True)
    if audit["status"] != "PASS":
        raise SourceError("Independent audit did not pass")
    raw = await cognee.search(
        query_text=query, query_type=SearchType.CHUNKS_LEXICAL, datasets=[dataset], top_k=top_k
    )
    return result_hits(raw, snapshot)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True, help="Local OpenSiro Index clone")
    parser.add_argument("--commit", required=True, help="Pinned 40-hex Index Git SHA")
    parser.add_argument("--store-root", type=Path, required=True, help="Existing import store")
    parser.add_argument("--dataset", required=True, help="Existing lexical import dataset")
    parser.add_argument("--query", required=True, help="Native Cognee section-level lexical query")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--inspect", help="Select harness ID displayed in native results")
    parser.add_argument("--show-markdown", action="store_true", help="Print full pinned source")
    args = parser.parse_args()
    if not args.dataset.strip() or not args.query.strip() or not 1 <= args.top_k <= 50:
        parser.error("Nonempty dataset/query and top-k between 1 and 50 required")
    if args.show_markdown and not args.inspect:
        parser.error("--show-markdown requires --inspect")
    try:
        snapshot = read_snapshot(args.repo, args.commit)
        configure_existing_store(args.store_root.resolve())
        hits = asyncio.run(search_existing(snapshot, args.dataset, args.query, args.top_k))
        result = {
            "dataset": args.dataset,
            "index_commit": args.commit,
            "query": args.query,
            "native_search": "SearchType.CHUNKS_LEXICAL",
            "search_scope": "six S1/S2/S3/S3*/S4/S5 sections per imported assessment; not full-body",
            "all_native_hits_unfiltered": hits,
            "inspection": (
                source_detail(snapshot, args.inspect, hits, args.show_markdown)
                if args.inspect
                else None
            ),
            "warning": "Lexical results may be irrelevant; inspect pinned upstream code separately.",
        }
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except SourceError as exc:
        parser.exit(2, "FAIL_CLOSED: " + str(exc) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
