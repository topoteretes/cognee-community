"""The deliberately small compatibility boundary for Cognee 1.6.4 local stores.

No backend repair SQL lives here. Deletion belongs to Cognee's provenance
planner; this module inspects its outcome, including vectors after graph loss.
"""

from functools import wraps
from importlib.metadata import version
from pathlib import Path
from uuid import UUID

from .models import IncompleteSyncError, UnsupportedBackendError, VerificationError


def _dataset_context(function):
    @wraps(function)
    async def scoped(dataset, *args, **kwargs):
        from cognee.context_global_variables import set_database_global_context_variables

        async with set_database_global_context_variables(dataset.id, dataset.owner_id):
            return await function(dataset, *args, **kwargs)

    return scoped


def storage_namespace():
    from cognee.base_config import get_base_config

    return str(Path(get_base_config().system_root_directory).resolve())


def check_backends():
    from cognee.infrastructure.databases.graph.config import get_graph_config
    from cognee.infrastructure.databases.relational import get_relational_config
    from cognee.infrastructure.databases.vector.config import get_vectordb_config
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SYNC_VERSION

    if version("cognee") != "1.6.4" or DOCUMENT_SYNC_VERSION < 2:
        raise UnsupportedBackendError("This connector's verified contract requires Cognee 1.6.4")
    if (
        get_relational_config().db_provider != "sqlite"
        or get_graph_config().graph_database_provider != "ladybug"
        or get_vectordb_config().vector_db_provider != "lancedb"
    ):
        raise UnsupportedBackendError("Verified sync requires SQLite/Ladybug/LanceDB")


async def authorize(dataset_name, user=None):
    from cognee.modules.migrations.startup import run_migrations_and_block
    from cognee.modules.pipelines.layers.resolve_authorized_user_dataset import (
        resolve_authorized_user_dataset,
    )

    await run_migrations_and_block(dataset_name, user)
    user, dataset = await resolve_authorized_user_dataset(dataset_name=dataset_name, user=user)
    # Cleanup is part of the operation, so write permission alone is insufficient.
    from cognee.modules.data.methods.get_authorized_dataset import get_authorized_dataset

    if await get_authorized_dataset(user, dataset.id, "delete") is None:
        raise IncompleteSyncError("Synchronization requires write and delete permission")
    return user, dataset


async def _engines(dataset):
    from cognee.infrastructure.databases.graph import get_graph_engine
    from cognee.infrastructure.databases.vector.get_vector_engine import get_vector_engine_async

    return await get_graph_engine(), await get_vector_engine_async()


@_dataset_context
async def release_handles(dataset):
    """Use core's cache API to release this physical store before process unlock.

    Closing a cached connection does not erase its data. Re-resolving core
    handles remain usable; no cache for another database is cleared.
    """
    import gc

    from cognee.infrastructure.databases.graph.config import get_graph_context_config
    from cognee.infrastructure.databases.graph.get_graph_engine import (
        _create_graph_engine,
        graph_engine_cache,
    )
    from cognee.infrastructure.databases.vector.config import get_vectordb_context_config
    from cognee.infrastructure.databases.vector.create_vector_engine import (
        _create_vector_engine,
        vector_engine_cache,
    )

    gc.collect()
    graph_config, vector_config = get_graph_context_config(), get_vectordb_context_config()
    graph_engine_cache.evict(force_close=True, **graph_config)
    vector_engine_cache.evict(force_close=True, **vector_config)
    if graph_config.get("graph_database_name"):
        await graph_engine_cache.aevict_for_database(graph_config["graph_database_name"])
    else:
        await _create_graph_engine.cache_await_closed(
            graph_file_path=graph_config.get("graph_file_path")
        )
    if vector_config.get("vector_db_name"):
        await vector_engine_cache.aevict_for_database(vector_config["vector_db_name"])
    else:
        await _create_vector_engine.cache_await_closed(
            vector_db_url=vector_config.get("vector_db_url")
        )


@_dataset_context
async def scoped_data(dataset, table_name):
    from cognee.modules.data.methods import get_dataset_data

    return [
        item
        for item in await get_dataset_data(dataset.id)
        if isinstance(item.system_metadata, dict)
        and item.system_metadata.get("source") == "read_ai"
        and item.system_metadata.get("table_name", "").split(".")[-1] == table_name
    ]


def pipeline_for(source, dataset_name):
    import dlt
    from cognee.tasks.ingestion.dlt_utils import pipeline_name_for_source
    from cognee.tasks.ingestion.get_dlt_destination import get_dlt_destination
    from cognee.tasks.ingestion.ingest_dlt_source import _to_safe_ident

    safe = _to_safe_ident(dataset_name)
    return dlt.pipeline(
        pipeline_name=pipeline_name_for_source(source, dataset_name),
        destination=get_dlt_destination(f"dlt_database_{safe}"),
        dataset_name=safe,
    )


def recover_packages(source, dataset_name):
    """Abort only a private pipeline's pending work; never promise SQL rollback."""
    pipeline = pipeline_for(source, dataset_name)
    if not pipeline.has_pending_data:
        return False
    pipeline.abort_packages()
    # Finish persisted abort intents without introducing a fresh source.
    pipeline.load()
    if pipeline.has_pending_data:
        raise IncompleteSyncError("Owned dlt packages could not be aborted")
    return True


async def add_rows(source, dataset_name, user, write_disposition="merge"):
    import cognee
    from cognee.modules.pipelines.models import PipelineRunAlreadyCompleted, PipelineRunCompleted

    result = await cognee.add(
        source,
        dataset_name=dataset_name,
        user=user,
        dlt_config={"write_disposition": write_disposition, "primary_key": "id"},
        run_in_background=False,
    )
    if not isinstance(result, (PipelineRunCompleted, PipelineRunAlreadyCompleted)):
        raise IncompleteSyncError("Cognee add did not complete; staging is not sync success")
    return result


async def cognify(dataset_id, user):
    import cognee
    from cognee.modules.pipelines.models import PipelineRunAlreadyCompleted, PipelineRunCompleted

    result = await cognee.cognify(
        datasets=[dataset_id], user=user, run_in_background=False, raise_on_error=True
    )
    run = (
        result.get(dataset_id, result.get(str(dataset_id))) if isinstance(result, dict) else result
    )
    if not isinstance(run, (PipelineRunCompleted, PipelineRunAlreadyCompleted)):
        raise IncompleteSyncError("Cognee cognify did not report a completed dataset run")
    return result


def _owns(ref, dataset_id, data_ids):
    from cognee.infrastructure.databases.provenance import parse_source_ref_key

    parsed = parse_source_ref_key(ref)
    return str(parsed.dataset_id) == str(dataset_id) and str(parsed.data_id) in data_ids


@_dataset_context
async def capture_artifacts(dataset, data_ids):
    graph, _ = await _engines(dataset)
    from cognee.infrastructure.databases.provenance.markers import stores_provenance_in_graph

    data_ids = set(map(str, data_ids))
    if not await stores_provenance_in_graph(graph):
        if await graph.get_graph_counts() != (0, 0):
            raise UnsupportedBackendError(
                "Verified synchronization requires an empty or graph-provenance Cognee graph"
            )
        return {"nodes": [], "edges": [], "data_ids": sorted(data_ids)}
    refs = await graph.find_node_source_refs_by_dataset(str(dataset.id))
    ids = [
        key for key, values in refs.items() if any(_owns(v, dataset.id, data_ids) for v in values)
    ]
    nodes = await graph.get_node_delete_data(ids) if ids else {}
    edge_refs = await graph.find_edge_source_refs_by_dataset(str(dataset.id))
    edges = [
        key
        for key, values in edge_refs.items()
        if any(_owns(v, dataset.id, data_ids) for v in values)
    ]
    edge_data = await graph.get_edge_delete_data(edges) if edges else {}
    return {
        "data_ids": sorted(data_ids),
        "nodes": [
            {
                "id": key,
                "type": value.node_type,
                "fields": value.indexed_fields,
                "refs": value.source_ref_keys,
            }
            for key, value in nodes.items()
        ],
        "edges": [
            {
                "source": key.source_id,
                "target": key.target_id,
                "relationship": key.relationship_name,
                "text": value.edge_text,
                "refs": value.source_ref_keys,
            }
            for key, value in edge_data.items()
        ],
    }


def merge_artifacts(*snapshots):
    nodes, edges = {}, {}
    owned_ids = set()
    for snapshot in snapshots:
        owned_ids.update((snapshot or {}).get("data_ids", []))
        for node in (snapshot or {}).get("nodes", []):
            previous = nodes.get(node["id"])
            if previous:
                node = {**node, "refs": sorted(set(node["refs"] + previous["refs"]))}
            nodes[node["id"]] = node
        for edge in (snapshot or {}).get("edges", []):
            key = (edge["source"], edge["target"], edge["relationship"])
            previous = edges.get(key)
            if previous:
                edge = {**edge, "refs": sorted(set(edge["refs"] + previous["refs"]))}
            edges[key] = edge
    return {
        "nodes": list(nodes.values()),
        "edges": list(edges.values()),
        "data_ids": sorted(owned_ids),
    }


async def _vector_exists(vector, collection, node_id):
    if not await vector.has_collection(collection):
        return False
    return bool(await vector.retrieve(collection, [node_id]))


@_dataset_context
async def verify_corpus(dataset, table_name, expected_rows, prior_artifacts):
    """Inspect actual documents and source refs; no model calls or delete workarounds."""
    from cognee.infrastructure.databases.provenance import EdgeIdentity
    from cognee.infrastructure.files.utils.open_data_file import open_data_file
    from cognee.modules.engine.utils import generate_node_id
    from cognee.modules.graph.models.EdgeType import EdgeType
    from cognee.modules.graph.utils.prepare_edges_for_storage import get_edge_retrieval_text

    records = await scoped_data(dataset, table_name)
    indexed = {}
    for item in records:
        key = item.system_metadata.get("external_id")
        if key in indexed:
            raise VerificationError("Duplicate meeting versions remain in the owned corpus")
        indexed[key] = item
    if set(indexed) != set(expected_rows):
        raise VerificationError("Stored meeting identities do not match the prepared corpus")
    for key, row in expected_rows.items():
        expected = (
            f"# {row['title']}\n\n{row['content']}" if row["title"] else row["content"]
        ).strip()
        async with open_data_file(
            indexed[key].raw_data_location, mode="r", encoding="utf-8"
        ) as stream:
            actual = stream.read()
        if actual != expected:
            raise VerificationError("Stored meeting text does not match selected current content")

    current_ids = {str(value.id) for value in records}
    graph, vector = await _engines(dataset)
    current = await capture_artifacts(dataset, current_ids)
    for record in records:
        chunks = [
            node
            for node in current["nodes"]
            if node["type"] == "DocumentChunk"
            and any(_owns(ref, dataset.id, {str(record.id)}) for ref in node["refs"])
        ]
        if not chunks:
            raise VerificationError("A current meeting has no cognified document chunks")
        for node in chunks:
            if "text" not in node["fields"] or not await _vector_exists(
                vector, "DocumentChunk_text", node["id"]
            ):
                raise VerificationError("A current meeting chunk has no indexed vector")

    def stale(ref):
        from cognee.infrastructure.databases.provenance import parse_source_ref_key

        parsed = parse_source_ref_key(ref)
        return (
            str(parsed.dataset_id) == str(dataset.id)
            and str(parsed.data_id) in set(prior_artifacts.get("data_ids", []))
            and str(parsed.data_id) not in current_ids
        )

    for node in prior_artifacts.get("nodes", []):
        old_refs = [ref for ref in node["refs"] if stale(ref)]
        if not old_refs:
            continue
        live = (await graph.get_node_delete_data([node["id"]])).get(node["id"])
        if live:
            if any(ref in live.source_ref_keys for ref in old_refs):
                raise VerificationError("Stale document/chunk provenance remains in the graph")
            if not live.source_ref_keys:
                raise VerificationError("An unowned graph artifact survived cleanup")
            remaining_refs = {ref for ref in node["refs"] if not stale(ref)}
            if not remaining_refs.issubset(live.source_ref_keys):
                raise VerificationError("Cleanup removed a surviving document's ownership")
            for name in node["fields"]:
                if not await _vector_exists(vector, f"{node['type']}_{name}", node["id"]):
                    raise VerificationError("Cleanup removed a shared artifact's vector")
        else:
            if any(not stale(ref) for ref in node["refs"]):
                raise VerificationError("Cleanup removed an artifact supported by other content")
            for name in node["fields"]:
                if await _vector_exists(vector, f"{node['type']}_{name}", node["id"]):
                    raise VerificationError("A stale document/chunk/entity vector survived cleanup")
    for edge in prior_artifacts.get("edges", []):
        old_refs = [ref for ref in edge["refs"] if stale(ref)]
        if not old_refs:
            continue
        identity = EdgeIdentity(edge["source"], edge["target"], edge["relationship"])
        live = (await graph.get_edge_delete_data([identity])).get(identity)
        if live and any(ref in live.source_ref_keys for ref in old_refs):
            raise VerificationError("Stale edge provenance survived cleanup")
        if live and (
            not live.source_ref_keys
            or not {ref for ref in edge["refs"] if not stale(ref)}.issubset(live.source_ref_keys)
        ):
            raise VerificationError("Cleanup removed a shared edge's ownership")
        if not live:
            if any(not stale(ref) for ref in edge["refs"]):
                raise VerificationError("Cleanup removed a shared edge")
            triplet_id = str(
                generate_node_id(edge["source"] + edge["relationship"] + edge["target"])
            )
            if await _vector_exists(vector, "Triplet_text", triplet_id):
                raise VerificationError("A stale triplet vector survived cleanup")
            text = get_edge_retrieval_text(edge["text"], edge["relationship"])
            # EdgeType vectors are shared by relationship text; retain when an
            # edge anywhere still supports that text, like Cognee's planner.
            _, remaining = await graph.get_graph_data()
            supported = any(
                get_edge_retrieval_text(e[3].get("edge_text"), e[2]) == text for e in remaining
            )
            if not supported and await _vector_exists(
                vector, "EdgeType_relationship_name", str(EdgeType.id_for(text))
            ):
                raise VerificationError("An orphaned EdgeType vector survived cleanup")
    return records, current


@_dataset_context
async def repair_record_cleanup(dataset, old_data_ids, current_data_ids, user):
    """Retry Cognee's existing deletion API, never delete backend objects directly."""
    import cognee

    for value in sorted(set(old_data_ids) - set(current_data_ids)):
        await cognee.datasets.delete_data(dataset.id, UUID(value), user=user, mode="soft")
