"""Offline Graph API, real Cognee ingestion, SQLite, Ladybug and LanceDB.

These checks establish local persistence and recovery, not Microsoft service
behavior. Only the provider responses and external language-model calls are fake.
"""

import hashlib
import math
import re
import sqlite3
from unittest.mock import patch

import cognee
import httpx
import pytest
import pytest_asyncio

from cognee_community_connector_onenote import onenote_source

DATASET = "onenote_local_integration"


class GraphPages:
    def __init__(self):
        self.pages = {"page-a": "alpha payments restart runbook"}
        self.timestamps = {"page-a": "2026-01-01T00:00:00Z"}
        self.html_calls = 0

    def handler(self, request):
        path = request.url.path.removeprefix("/v1.0/me/onenote/")
        if path == "notebooks":
            return httpx.Response(200, json={"value": [{"id": "book", "displayName": "Project"}]})
        if path == "notebooks/book/sections":
            return httpx.Response(
                200, json={"value": [{"id": "section", "displayName": "Runbooks"}]}
            )
        if path == "notebooks/book/sectionGroups":
            return httpx.Response(200, json={"value": []})
        if path == "sections/section/pages":
            return httpx.Response(200, json={"value": [self.metadata(p) for p in self.pages]})
        if path.startswith("pages/"):
            page = path.split("/")[1]
            if page not in self.pages:
                return httpx.Response(404, json={"error": {"code": "20113"}})
            if path.endswith("/content"):
                self.html_calls += 1
                return httpx.Response(
                    200,
                    text=f"<html><body><p>{self.pages[page]}</p></body></html>",
                    headers={"content-type": "text/html"},
                )
            return httpx.Response(200, json=self.metadata(page))
        raise AssertionError(f"Unexpected Graph request: {request.url}")

    def metadata(self, page):
        return {
            "id": page,
            "title": page,
            "lastModifiedDateTime": self.timestamps[page],
            "parentNotebook": {"id": "book"},
            "parentSection": {"id": "section"},
            "links": {"oneNoteWebUrl": {"href": f"https://example.com/notes/{page}"}},
        }

    def source(self):
        return onenote_source(
            "offline-token",
            notebook_ids=["book"],
            account_id="tenant:local-account",
            http_client=httpx.Client(transport=httpx.MockTransport(self.handler)),
        )


@pytest_asyncio.fixture
async def persistent_environment(tmp_path, monkeypatch):
    from cognee.context_global_variables import graph_db_config, vector_db_config
    from cognee.infrastructure.databases.graph.get_graph_engine import _create_graph_engine
    from cognee.infrastructure.databases.relational.create_relational_engine import (
        create_relational_engine,
    )
    from cognee.infrastructure.databases.vector.create_vector_engine import _create_vector_engine
    from cognee.modules.engine.operations.setup import setup
    from cognee.tasks.ingestion.get_dlt_destination import get_dlt_destination
    from dlt.common.configuration.container import Container
    from dlt.common.pipeline import PipelineContext

    Container()[PipelineContext].deactivate()
    for key, value in {
        "COGNEE_SKIP_CONNECTION_TEST": "true",
        "ENABLE_BACKEND_ACCESS_CONTROL": "true",
        "GRAPH_DATASET_DATABASE_HANDLER": "ladybug",
        "VECTOR_DATASET_DATABASE_HANDLER": "lancedb",
        "LLM_API_KEY": "sk-offline",
        "MOCK_EMBEDDING": "true",
        "EMBEDDING_PROVIDER": "openai",
        "EMBEDDING_MODEL": "openai/text-embedding-3-large",
        "EMBEDDING_DIMENSIONS": "384",
        "DLT_DATA_DIR": str(tmp_path / "dlt"),
        "PIPELINES_DIR": str(tmp_path / "dlt" / "pipelines"),
    }.items():
        monkeypatch.setenv(key, value)
    for factory in (
        _create_graph_engine,
        _create_vector_engine,
        create_relational_engine,
        get_dlt_destination,
    ):
        factory.cache_clear()
    graph_db_config.set(None)
    vector_db_config.set(None)
    cognee.config.set_graph_db_config(
        {"graph_database_provider": "ladybug", "graph_dataset_database_handler": "ladybug"}
    )
    cognee.config.set_vector_db_config(
        {"vector_db_provider": "lancedb", "vector_dataset_database_handler": "lancedb"}
    )
    cognee.config.set_relational_db_config({"db_provider": "sqlite"})
    cognee.config.set_migration_db_config({"migration_db_provider": "sqlite"})
    cognee.config.system_root_directory(str(tmp_path / "system"))
    cognee.config.data_root_directory(str(tmp_path / "data"))
    cognee.config.set_vector_db_url(str(tmp_path / "system" / "databases" / "vectors"))
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    await setup()

    async def token_embeddings(self, texts):
        vectors = []
        for text in texts:
            vector = [0.0] * 384
            for token in re.findall(r"\w+", text.lower()):
                index = int(hashlib.sha256(token.encode()).hexdigest()[:8], 16) % 384
                vector[index] += 1.0
            norm = math.sqrt(sum(value * value for value in vector)) or 1
            vectors.append([value / norm for value in vector])
        return vectors

    with patch(
        "cognee.infrastructure.databases.vector.embeddings.LiteLLMEmbeddingEngine."
        "LiteLLMEmbeddingEngine.embed_text",
        token_embeddings,
    ):
        yield tmp_path
    Container()[PipelineContext].deactivate()
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)


def model_calls():
    from cognee.shared.data_models import Edge, KnowledgeGraph, Node, SummarizedContent

    async def output(text_input, system_prompt, response_model, **kwargs):
        if response_model.__name__ == "KnowledgeGraph":
            identity = hashlib.sha256((text_input or "").encode()).hexdigest()[:12]
            return KnowledgeGraph(
                nodes=[
                    Node(
                        id="shared", name="payments", type="Concept", description="shared service"
                    ),
                    Node(id=identity, name=identity, type="Concept", description="page detail"),
                ],
                edges=[
                    Edge(source_node_id=identity, target_node_id="shared", relationship_name="uses")
                ],
            )
        if response_model.__name__ == "SummarizedContent":
            return SummarizedContent(summary=(text_input or "")[:120], description="")
        return response_model()

    return patch(
        "cognee.infrastructure.llm.LLMGateway.LLMGateway.acreate_structured_output",
        side_effect=output,
    )


async def records_and_dataset():
    from cognee.modules.data.methods import get_authorized_existing_datasets
    from cognee.modules.data.methods.get_dataset_data import get_dataset_data
    from cognee.modules.users.methods import get_default_user

    dataset = (
        await get_authorized_existing_datasets(
            user=await get_default_user(), permission_type="read", datasets=[DATASET]
        )
    )[0]
    return await get_dataset_data(dataset.id), dataset


async def stores(dataset):
    from cognee.context_global_variables import set_database_global_context_variables
    from cognee.infrastructure.databases.graph import get_graph_engine
    from cognee.infrastructure.databases.vector import get_vector_engine_async

    async with set_database_global_context_variables(dataset.id, dataset.owner_id):
        graph = await get_graph_engine()
        nodes, edges = await graph.get_graph_data()
        vector = await get_vector_engine_async()
        collection = await vector.get_collection("DocumentChunk_text")
        chunks = await collection.query().to_list()
    return nodes, edges, chunks


async def all_vector_rows(dataset):
    from cognee.context_global_variables import set_database_global_context_variables
    from cognee.infrastructure.databases.vector import get_vector_engine_async

    async with set_database_global_context_variables(dataset.id, dataset.owner_id):
        vector = await get_vector_engine_async()
        connection = await vector.get_connection()
        return {
            name: await (await connection.open_table(name)).query().to_list()
            for name in await connection.table_names()
        }


async def add_snapshot(fake):
    from cognee.modules.pipelines.models import PipelineRunAlreadyCompleted, PipelineRunCompleted

    result = await cognee.add(fake.source(), dataset_name=DATASET)
    runs = list(result.values()) if isinstance(result, dict) else [result]
    assert runs and all(
        isinstance(run, (PipelineRunCompleted, PipelineRunAlreadyCompleted)) for run in runs
    ), result
    return result


@pytest.mark.asyncio
async def test_final_page_deletion_physically_clears_owned_stores(persistent_environment):
    from cognee.infrastructure.databases.relational import get_relational_config

    fake = GraphPages()
    with model_calls() as language_model:
        await add_snapshot(fake)
        await cognee.cognify(datasets=[DATASET])
        first, dataset = await records_and_dataset()
        assert len(first) == 1
        nodes, edges, chunks = await stores(dataset)
        assert nodes and edges and chunks
        vectors_before = await all_vector_rows(dataset)
        assert vectors_before["DocumentChunk_text"]
        assert any(rows for name, rows in vectors_before.items() if name != "DocumentChunk_text")
        assert any("alpha payments" in str(row) for row in chunks)
        from cognee.modules.search.types import SearchType

        recalled = await cognee.recall(
            "alpha payments restart",
            query_type=SearchType.CHUNKS,
            datasets=[DATASET],
            auto_route=False,
        )
        assert "alpha payments" in str(recalled)
        original_ids = {str(record.id) for record in first}
        model_count = language_model.call_count
        await add_snapshot(fake)
        await cognee.cognify(datasets=[DATASET])
        unchanged, _ = await records_and_dataset()
        assert {str(record.id) for record in unchanged} == original_ids
        assert fake.html_calls == 1
        assert language_model.call_count == model_count

        fake.pages.clear()
        await add_snapshot(fake)
        after, _ = await records_and_dataset()
        assert after == []
        after_nodes, after_edges, after_chunks = await stores(dataset)
        assert after_nodes == []
        assert after_edges == []
        assert after_chunks == []
        vectors_after = await all_vector_rows(dataset)
        assert vectors_after.keys() == vectors_before.keys()
        assert all(rows == [] for rows in vectors_after.values()), vectors_after

    db = get_relational_config()
    staging = f"{db.db_path}/dlt_database_{DATASET}__{DATASET}"
    with sqlite3.connect(staging) as connection:
        tables = [
            r[0]
            for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if r[0].startswith("onenote_")
        ]
        assert len(tables) == 1
        assert connection.execute(f'SELECT count(*) FROM "{tables[0]}"').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_unchanged_snapshot_recovers_after_model_failure(persistent_environment):
    fake = GraphPages()
    await add_snapshot(fake)
    with patch(
        "cognee.infrastructure.llm.LLMGateway.LLMGateway.acreate_structured_output",
        side_effect=RuntimeError("injected model failure after staging"),
    ):
        with pytest.raises(Exception, match="injected model failure"):
            await cognee.cognify(datasets=[DATASET], raise_on_error=True)
    staged, _ = await records_and_dataset()
    assert len(staged) == 1
    with model_calls():
        await add_snapshot(fake)
        await cognee.cognify(datasets=[DATASET], raise_on_error=True)
    recovered, dataset = await records_and_dataset()
    assert {record.id for record in recovered} == {record.id for record in staged}
    assert fake.html_calls == 1
    nodes, edges, chunks = await stores(dataset)
    assert nodes and edges and chunks
    assert any("alpha payments" in str(row) for row in chunks)


@pytest.mark.asyncio
async def test_empty_onenote_retains_other_source_and_shared_entity(persistent_environment):
    import dlt
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR

    @dlt.resource(name="other_documents", primary_key="id", write_disposition="replace")
    def other_documents():
        yield {
            "id": "other-page",
            "title": "Other source",
            "content": "beta payments onboarding asks the service team",
            "url": "https://example.com/other",
        }

    other = other_documents()
    setattr(other, DOCUMENT_SOURCE_ATTR, "other_connector")
    setattr(other, PIPELINE_SCOPE_ATTR, "other_connector")
    fake = GraphPages()
    with model_calls():
        await add_snapshot(fake)
        await cognee.add(other, dataset_name=DATASET)
        await cognee.cognify(datasets=[DATASET])
        before, dataset = await records_and_dataset()
        assert len(before) == 2
        survivor = next(
            record for record in before if record.system_metadata["source"] == "other_connector"
        )
        deleted = next(record for record in before if record.id != survivor.id)
        nodes, edges, chunks = await stores(dataset)
        assert len(chunks) == 2
        shared_ids = {
            node_id for node_id, properties in nodes if properties.get("name") == "payments"
        }
        assert shared_ids
        fake.pages.clear()
        await add_snapshot(fake)

    after, _ = await records_and_dataset()
    assert {record.id for record in after} == {survivor.id}
    after_nodes, after_edges, after_chunks = await stores(dataset)
    node_ids = {node_id for node_id, _ in after_nodes}
    assert str(deleted.id) not in node_ids
    assert str(survivor.id) in node_ids
    assert shared_ids <= node_ids
    assert after_edges
    assert len(after_chunks) == 1
    assert "beta payments" in str(after_chunks[0])
    assert "alpha payments" not in str(after_chunks[0])
    assert all(str(deleted.id) not in (source, target) for source, target, _, _ in after_edges)


@pytest.mark.asyncio
async def test_cleanup_failure_retries_on_next_empty_snapshot(persistent_environment):
    import importlib

    fake = GraphPages()
    with model_calls():
        await add_snapshot(fake)
        await cognee.cognify(datasets=[DATASET])
    original, dataset = await records_and_dataset()
    assert len(original) == 1
    fake.pages.clear()
    delete_module = importlib.import_module("cognee.modules.data.methods.delete_data")
    with patch.object(
        delete_module, "delete_data", side_effect=RuntimeError("injected cleanup failure")
    ) as delete:
        await add_snapshot(fake)
        assert delete.called
    retained, _ = await records_and_dataset()
    assert {record.id for record in retained} == {record.id for record in original}
    await add_snapshot(fake)
    assert (await records_and_dataset())[0] == []
    nodes, edges, chunks = await stores(dataset)
    assert nodes == []
    assert edges == []
    assert chunks == []
    assert all(rows == [] for rows in (await all_vector_rows(dataset)).values())


@pytest.mark.asyncio
async def test_timestamp_only_and_real_edit_reconcile_document_artifacts(persistent_environment):
    from cognee.modules.search.types import SearchType

    fake = GraphPages()
    with model_calls() as language_model:
        await add_snapshot(fake)
        await cognee.cognify(datasets=[DATASET])
        original, dataset = await records_and_dataset()
        assert len(original) == 1
        original_id = original[0].id
        original_nodes, original_edges, original_chunks = await stores(dataset)
        original_vectors = await all_vector_rows(dataset)
        original_chunk_ids = {str(row["id"]) for row in original_chunks}
        assert original_chunk_ids
        initial_model_count = language_model.call_count

        # Provider timestamp churn must not change emitted content or identities.
        fake.timestamps["page-a"] = "2026-01-02T00:00:00Z"
        await add_snapshot(fake)
        await cognee.cognify(datasets=[DATASET])
        timestamp_only, _ = await records_and_dataset()
        assert {record.id for record in timestamp_only} == {original_id}
        assert fake.html_calls == 2
        assert language_model.call_count == initial_model_count
        assert (await stores(dataset))[2] == original_chunks

        fake.pages["page-a"] = "gamma observatory telescope calibration procedure"
        fake.timestamps["page-a"] = "2026-01-03T00:00:00Z"
        await add_snapshot(fake)
        await cognee.cognify(datasets=[DATASET])

        updated, _ = await records_and_dataset()
        assert len(updated) == 1
        assert updated[0].id != original_id
        assert updated[0].system_metadata["external_id"] == "page-a"
        assert fake.html_calls == 3
        assert language_model.call_count > initial_model_count
        updated_nodes, updated_edges, updated_chunks = await stores(dataset)
        updated_node_ids = {node_id for node_id, _ in updated_nodes}
        assert str(original_id) not in updated_node_ids
        assert str(updated[0].id) in updated_node_ids
        assert not original_chunk_ids.intersection(updated_node_ids)
        assert all(
            str(original_id) not in (source, target) for source, target, _, _ in updated_edges
        )
        assert "gamma observatory" in str(updated_chunks)
        assert "alpha payments" not in str(updated_chunks)
        updated_vectors = await all_vector_rows(dataset)
        updated_vector_ids = {str(row["id"]) for rows in updated_vectors.values() for row in rows}
        assert not original_chunk_ids.intersection(updated_vector_ids)
        # All artifacts exclusively supported by the old document disappear;
        # a concept shared by both extractions may retain its stable identity.
        original_node_ids = {node_id for node_id, _ in original_nodes}
        shared_ids = {
            node_id
            for node_id, properties in original_nodes
            if properties.get("name") == "payments"
        }
        assert original_edges
        assert original_vectors
        from cognee.modules.engine.models import EntityType

        # The shared concept and its type are emitted by both model results.
        # Their deterministic identities are expected to be rebuilt or retained.
        shared_ids.add(str(EntityType.id_for("Concept")))
        exclusive_ids = original_node_ids - shared_ids
        assert not exclusive_ids.intersection(updated_node_ids)
        assert not exclusive_ids.intersection(updated_vector_ids)
        result = await cognee.recall(
            "gamma observatory telescope",
            query_type=SearchType.CHUNKS,
            datasets=[DATASET],
            auto_route=False,
        )
        assert "gamma observatory" in str(result)
        assert "alpha payments" not in str(result)
