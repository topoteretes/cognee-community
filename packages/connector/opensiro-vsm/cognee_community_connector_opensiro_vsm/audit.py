"""Read-only independent audit of a freshly imported Cognee dataset.

Assumptions: snapshot already validated from Git tree and all selected records
belong to snapshot.accepted. Supports v1 document source refs and v2 chunk refs.
No writes to target Cognee dataset.
"""

from __future__ import annotations

import json
from collections import Counter

import cognee
from cognee.context_global_variables import set_database_global_context_variables
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.infrastructure.databases.provenance import make_source_ref_key
from cognee.modules.search.types import SearchType

from .native import source_data
from .source import Snapshot, SourceError

AUDIT_SCHEMA = "opensiro-vsm-independent-audit/v1"


def verify_node_type_counts(observed: Counter, expected: dict[str, int]) -> None:
    """Compare graph types exactly, treating absent zero-count types as zero."""
    unexpected = {kind: amount for kind, amount in observed.items() if kind not in expected}
    mismatched = {
        kind: {"actual": observed.get(kind, 0), "expected": amount}
        for kind, amount in expected.items()
        if observed.get(kind, 0) != amount
    }
    if unexpected or mismatched:
        raise SourceError(
            f"Unexpected typed graph counts: unexpected={unexpected}, mismatched={mismatched}"
        )


def source_key(node_id: str, fields: dict):
    kind = fields.get("type")
    if kind == "VSMAssessment":
        return "opensiro/vsm-harness-index:" + fields["harness_id"]
    if kind == "TextDocument":
        title = fields.get("name") or ""
        if not title.endswith(".md"):
            raise SourceError("TextDocument missing canonical Markdown filename")
        return "opensiro/vsm-harness-index:" + title[:-3]
    if kind == "DocumentChunk":
        return fields["document_id"]
    key = fields.get("key")
    if not isinstance(key, str):
        raise SourceError(f"node {node_id} has no stable key")
    if kind == "VSMHarness":
        return key
    components = key.split(":", 2)
    if len(components) < 3 or components[0] != "opensiro/vsm-harness-index":
        raise SourceError(f"node {node_id} has malformed versioned key")
    return components[0] + ":" + components[1]


async def audit_dataset(
    snapshot: Snapshot,
    selected: tuple[str, ...],
    dataset: str,
    tag: str,
    *,
    lexical: bool,
):
    if not selected or len(selected) != len(set(selected)):
        raise SourceError("Audit selection empty or duplicated")
    if any(identifier not in snapshot.accepted for identifier in selected):
        raise SourceError("Audit selection contains non-admitted source identities")
    chosen = [snapshot.accepted[x] for x in selected]
    user, ds, data_rows, docs = await source_data(dataset, tag, existing_only=True)
    if len(docs) != len(data_rows) or len(docs) != len(chosen):
        raise SourceError("Missing or extraneous source Data rows in staged dataset")
    if set(docs) != {x.external_id for x in chosen}:
        raise SourceError("Dataset source identity differs from pinned selection")
    async with set_database_global_context_variables(ds.id, user.id, permission_type="read"):
        graph = await get_graph_engine()
        nodes, edges = await graph.get_graph_data()
        node_refs = await graph.find_node_source_refs_by_dataset(str(ds.id))
        edge_refs = await graph.find_edge_source_refs_by_dataset(str(ds.id))

    count = Counter(props.get("type") for _, props in nodes)
    expected = {
        "VSMHarness": len(chosen),
        "VSMAssessment": len(chosen),
        "VSMFunction": len(chosen) * 6,
        "VSMEvidence": sum(len(x.primary_evidence) for x in chosen),
    }
    if lexical:
        expected["DocumentChunk"] = len(chosen) * 6
        expected["TextDocument"] = len(chosen) * 6
    verify_node_type_counts(count, expected)
    node_map = {str(node_id): p for node_id, p in nodes}
    if len(node_refs) != len(nodes) or len(edge_refs) != len(edges):
        raise SourceError("Missing native source refs")
    ref_for_node = {}
    for nid, properties in node_map.items():
        external = source_key(nid, properties)
        data = docs.get(external)
        if data is None:
            raise SourceError(f"Graph source absent from dataset: {external}")
        # Cohesive v2 subref points at the exact owning Data and native chunk id.
        kind = properties.get("type")
        expected_ref = (
            f"source_ref:v2:{ds.id}:{data.id}:{nid}"
            if kind == "DocumentChunk"
            else make_source_ref_key(ds.id, data.id)
        )
        if node_refs.get(nid) != [expected_ref]:
            raise SourceError(f"Node reference inconsistent with source {external}")
        ref_for_node[nid] = expected_ref
        if kind == "VSMAssessment":
            record = snapshot.accepted[properties["harness_id"]]
            if (
                properties["content_sha256"] != record.sha256
                or properties["reviewed_upstream_ref"] != record.review_ref
                or properties["index_commit"] != snapshot.commit
            ):
                raise SourceError(f"Staged assessment provenance mismatch: {record.harness_id}")
    for edge, refs in edge_refs.items():
        if str(edge.source_id) not in ref_for_node:
            raise SourceError("Edge source node absent")
        if refs != [ref_for_node[str(edge.source_id)]]:
            raise SourceError("Edge native ownership differs from source node")

    qry = "MATCH (n:Node) WHERE n.type = 'VSMFunction' RETURN n.properties AS props"
    response = await cognee.search(query_text=qry, query_type=SearchType.CYPHER, datasets=[dataset])
    rows = [json.loads(r[0]) for r in response[0]["search_result"]]
    expected_grades = {
        r.version_id + ":" + fn: (fn, value) for r in chosen for fn, value in r.states
    }
    seen = {r["key"]: (r["function"], r["autonomy"]) for r in rows}
    if len(rows) != len(expected_grades) or seen != expected_grades:
        raise SourceError("Function state mismatch from actual Cognee public search")
    return {
        "schema": AUDIT_SCHEMA,
        "status": "PASS",
        "index_commit": snapshot.commit,
        "dataset": dataset,
        "source_tag": tag,
        "documents": len(docs),
        "accepted_harnesses": len(chosen),
        "exact_grades": len(rows),
        "states_exact": True,
        "source_refs_exact": True,
        "graph_nodes": len(nodes),
        "graph_edges": len(edges),
        "node_types": dict(count),
        "all_validated_sources": sorted(r.harness_id for r in chosen),
        "provenance_document_v1": sum(p.get("type") != "DocumentChunk" for _, p in nodes),
        "provenance_chunk_v2": sum(p.get("type") == "DocumentChunk" for _, p in nodes),
        "note": "One immutable local staged generation; no mutations performed during audit.",
    }
