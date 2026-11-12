"""Native Cognee projection for an isolated, previously empty local dataset.

Fresh-only graph_only=True; no LLM, embeddings or production refresh.
The source_snapshot is validated fully BEFORE Cognee is imported or written.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from uuid import uuid4

import cognee
import dlt
from cognee.context_global_variables import set_database_global_context_variables
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.infrastructure.databases.relational import get_async_session
from cognee.infrastructure.engine import DataPoint
from cognee.modules.chunking.models import DocumentChunk
from cognee.modules.data.models import Data
from cognee.modules.data.processing.document_types import TextDocument
from cognee.modules.pipelines.layers.resolve_authorized_user_dataset import (
    resolve_authorized_user_dataset,
)
from cognee.modules.pipelines.models.PipelineContext import PipelineContext
from cognee.modules.search.types import SearchType
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from cognee.tasks.storage import add_data_points
from sqlalchemy import select

from .source import Record, Snapshot, SourceError

SECTION = re.compile(r"(?m)^## (S[1-5]\*?) [—-][^\n]*$")
NEXT = re.compile(r"(?m)^##\s+")


# Keep the Cognee DataPoint field declaration intact: the SDK reads this
# mutable metadata default to construct native graph and identity fields.
# Ruff RUF012 is suppressed locally rather than changing SDK semantics.
class VSMFunction(DataPoint):
    key: str
    function: str
    autonomy: str
    metadata: dict = {"index_fields": [], "identity_fields": ["key"]}  # noqa: RUF012


class VSMEvidence(DataPoint):
    key: str
    text: str
    source_level: str
    metadata: dict = {"index_fields": [], "identity_fields": ["key"]}  # noqa: RUF012


class VSMAssessment(DataPoint):
    key: str
    harness_id: str
    index_commit: str
    reviewed_upstream_ref: str
    content_sha256: str
    profile_version: str
    procedure_version: str
    source_url: str
    function_states: list[VSMFunction]
    primary_evidence: list[VSMEvidence]
    metadata: dict = {"index_fields": [], "identity_fields": ["key"]}  # noqa: RUF012


class VSMHarness(DataPoint):
    key: str
    project_name: str
    repository: str
    assessments: list[VSMAssessment]
    metadata: dict = {"index_fields": [], "identity_fields": ["key"]}  # noqa: RUF012


def dlt_source(records: list[Record], tag: str):
    @dlt.resource(name="assessments", primary_key="id", write_disposition="replace")
    def resources():
        for r in records:
            yield {
                "id": r.external_id,
                "title": r.project_name + " VSM assessment",
                "url": r.assessment_url,
                "content": r.raw_bytes.decode("utf-8"),
            }

    item = resources()
    setattr(item, DOCUMENT_SOURCE_ATTR, tag)
    return item


def typed_model(r: Record, pinned_commit: str):
    a = VSMAssessment(
        key=r.version_id,
        harness_id=r.harness_id,
        index_commit=pinned_commit,
        reviewed_upstream_ref=r.review_ref,
        content_sha256=r.sha256,
        profile_version=r.profile_version,
        procedure_version=r.assessment_procedure_version,
        source_url=r.assessment_url,
        function_states=[
            VSMFunction(key=r.version_id + ":" + fn, function=fn, autonomy=grade)
            for fn, grade in r.states
        ],
        primary_evidence=[
            VSMEvidence(
                key=r.version_id + ":primary:" + str(i),
                text=text,
                source_level="assessment_primary_evidence_only",
            )
            for i, text in enumerate(r.primary_evidence)
        ],
    )
    return VSMHarness(
        key=r.external_id, project_name=r.project_name, repository=r.repository, assessments=[a]
    )


def chunks(r: Record):
    raw = r.raw_bytes.decode("utf-8")
    heads = list(SECTION.finditer(raw))
    if len(heads) != 6:
        raise SourceError(f"{r.harness_id}: expected six function headings")
    result = []
    for i, h in enumerate(heads):
        following = NEXT.search(raw, h.end())
        end = following.start() if following else len(raw)
        section = raw[h.start() : end].strip()
        text = f"Project: {r.project_name}\nRepository: {r.repository}\n{section}"
        doc = TextDocument(
            name=r.harness_id + ".md",
            raw_data_location=r.assessment_url,
            external_metadata="",
            mime_type="text/plain",
        )
        result.append(
            DocumentChunk(
                text=text,
                chunk_size=len(text.split()),
                chunk_index=i,
                cut_type="markdown_section",
                is_part_of=doc,
                contains=[],
                document_id=r.external_id,
                document_name=r.harness_id + ".md",
            )
        )
    return result


async def source_data(dataset: str, tag: str, *, existing_only: bool = False):
    if existing_only:
        # The normal Cognee resolver CREATES absent datasets; an audit must not.
        from cognee.modules.data.methods.get_datasets_by_name import get_datasets_by_name
        from cognee.modules.users.methods import get_default_user

        user = await get_default_user()
        matched = await get_datasets_by_name(dataset, user.id)
        if len(matched) != 1:
            raise SourceError("Audit requires exactly one existing locally owned dataset")
        ds = matched[0]
    else:
        user, ds = await resolve_authorized_user_dataset(dataset_name=dataset)
    async with get_async_session() as sess:
        rows = (await sess.execute(select(Data).where(Data.dataset_id == ds.id))).scalars().all()
    docs = {}
    for row in rows:
        meta = row.system_metadata or {}
        if meta.get("source") != tag:
            continue
        key = meta.get("external_id")
        if not key or key in docs:
            raise SourceError("duplicate or missing Cognee external source ID")
        docs[key] = row
    return user, ds, rows, docs


async def ingest_fresh(
    snapshot: Snapshot,
    selected: tuple[str, ...],
    dataset: str,
    tag: str,
    lexical: bool = False,
    *,
    attempt_root: Path,
):
    """Fresh-only ingestion with a required single-host attempt fence."""
    if not selected or len(selected) != len(set(selected)):
        raise SourceError("selected set empty or duplicated")
    if any(x not in snapshot.accepted for x in selected):
        raise SourceError("Selection contains a non-admitted source ID")
    records = [snapshot.accepted[x] for x in selected]
    if lexical:
        # Preflight all section layouts before the first SDK write.
        for record in records:
            chunks(record)
    # Claim before Cognee SDK setup; never delete the marker.
    from .attempt import claim_attempt

    claim_attempt(attempt_root, dataset, tag, snapshot.commit, selected)
    # A genuinely empty local store has no relational schema yet. Initialize
    # it explicitly before querying for existing Data; Cognee.add normally
    # does this internally, but our fresh-only guard runs earlier.
    from cognee.low_level import setup

    await setup()
    user, ds, old_rows, _ = await source_data(dataset, tag)
    if old_rows:
        raise SourceError("Refusing nonempty Cognee dataset; fresh-only experimental adapter")
    t0 = time.monotonic()
    run = await cognee.add(
        dlt_source(records, tag),
        dataset_name=dataset,
        primary_key="id",
        write_disposition="replace",
        max_rows_per_table=0,
        skip_connection_test=True,
    )
    user, ds, all_rows, docs = await source_data(dataset, tag)
    if len(all_rows) != len(records) or set(docs) != {r.external_id for r in records}:
        raise SourceError("Incomplete document admission; stopping before graph projection")
    async with set_database_global_context_variables(ds.id, user.id, permission_type="write"):
        for r in records:
            ctx = PipelineContext(
                user=user,
                dataset=ds,
                data_item=docs[r.external_id],
                pipeline_run_id=uuid4(),
                pipeline_name="opensiro_source_prototype",
            )
            await add_data_points([typed_model(r, snapshot.commit)], graph_only=True, ctx=ctx)
            if lexical:
                await add_data_points(chunks(r), graph_only=True, ctx=ctx)
    async with set_database_global_context_variables(ds.id, user.id, permission_type="read"):
        engine = await get_graph_engine()
        nodes, edges = await engine.get_graph_data()
        refs = await engine.find_node_source_refs_by_dataset(str(ds.id))
    counts = {}
    for _, props in nodes:
        name = props.get("type", "")
        counts[name] = counts.get(name, 0) + 1
    for kind, n in (
        ("VSMHarness", len(records)),
        ("VSMAssessment", len(records)),
        ("VSMFunction", len(records) * 6),
        ("VSMEvidence", sum(len(r.primary_evidence) for r in records)),
    ):
        if counts.get(kind, 0) != n:
            raise SourceError(f"graph audit failed for {kind}: {counts.get(kind)} vs {n}")
    if lexical and counts.get("DocumentChunk") != len(records) * 6:
        raise SourceError("DocumentChunk projection count mismatch")
    if len(refs) != len(nodes):
        raise SourceError("native source provenance incomplete")
    query = "MATCH (n:Node) WHERE n.type = 'VSMFunction' RETURN n.properties AS props"
    result = await cognee.search(query_text=query, query_type=SearchType.CYPHER, datasets=[dataset])
    found = [json.loads(row[0]) for row in result[0]["search_result"]]
    expected = {r.version_id + ":" + f: (f, val) for r in records for f, val in r.states}
    got = {r["key"]: (r["function"], r["autonomy"]) for r in found}
    if len(found) != len(expected) or got != expected:
        raise SourceError("native Cypher grading differs from pinned index")
    return {
        "status": "PASS_FRESH_LOCAL_COGNEE_IMPORT",
        "dataset": dataset,
        "index_commit": snapshot.commit,
        "documents": len(docs),
        "graph_nodes": len(nodes),
        "graph_edges": len(edges),
        "source_scoped_nodes": len(refs),
        "node_type_counts": counts,
        "graded_facts": len(found),
        "all_states_exact": True,
        "sdk_result": type(run).__name__,
        "elapsed_s": round(time.monotonic() - t0, 3),
        "limitations": (
            "Fresh-only graph_only smoke. No updates, interrupted rollback, embeddings or GPU."
        ),
    }
