"""Offline lifecycle tests with real SQLite, Ladybug, and LanceDB stores.

Only Contentful transport, LLM output, and embeddings are faked. The public
remember/search calls, dlt staging, indexing, provenance, and deletion are real.
"""

import importlib
import json
import re
import sqlite3
from pathlib import Path
from urllib.parse import unquote, urlsplit

import cognee
import pytest
import pytest_asyncio
from cognee.context_global_variables import (
    graph_db_config,
    set_database_global_context_variables,
    vector_db_config,
)
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.infrastructure.databases.vector import get_vector_engine_async
from cognee.infrastructure.databases.vector.embeddings.LiteLLMEmbeddingEngine import (
    LiteLLMEmbeddingEngine,
)
from cognee.infrastructure.llm import LLMGateway
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.engine.operations.setup import setup as engine_setup
from cognee.modules.search.types import SearchType
from cognee.modules.users.methods import get_default_user
from cognee.shared.data_models import Edge, KnowledgeGraph, Node, SummarizedContent
from fakes import FakeContentful

from cognee_community_connector_contentful import contentful_source

DATASET = "contentful_integration"


async def _offline_embeddings(self, texts):
    # Non-zero, fixed vectors keep the actual LanceDB indexing/search paths
    # deterministic; top_k below is large enough to inspect all small fixtures.
    return [[0.125] * self.dimensions for _ in texts]


async def _offline_llm(text_input, system_prompt, response_model, **kwargs):
    if response_model is SummarizedContent:
        return SummarizedContent(summary=text_input, description="Contentful fixture")
    if response_model is KnowledgeGraph:
        markers = sorted(set(re.findall(r"CATALOG[A-Z]+", text_input)))
        names = markers + (["SharedCatalog"] if markers else [])
        return KnowledgeGraph(
            nodes=[Node(id=name, name=name, type="Concept", description=name) for name in names],
            edges=[
                Edge(
                    source_node_id=marker,
                    target_node_id="SharedCatalog",
                    relationship_name="belongs_to_catalog",
                )
                for marker in markers
            ],
        )
    raise AssertionError(f"Unexpected LLM response model: {response_model}")


@pytest_asyncio.fixture
async def local_stores(tmp_path, monkeypatch):
    """Keep every pipeline, engine and database in this test's storage root."""
    from cognee.infrastructure.databases.graph.get_graph_engine import _create_graph_engine
    from cognee.infrastructure.databases.relational.create_relational_engine import (
        create_relational_engine,
    )
    from cognee.infrastructure.databases.vector.create_vector_engine import _create_vector_engine
    from cognee.tasks.ingestion.get_dlt_destination import get_dlt_destination
    from dlt.common.configuration.container import Container
    from dlt.common.pipeline import PipelineContext

    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
    monkeypatch.setenv("ENABLE_BACKEND_ACCESS_CONTROL", "true")
    monkeypatch.setenv("CACHING", "false")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "databases"))
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt"))
    monkeypatch.setenv("PIPELINES_DIR", str(tmp_path / "dlt" / "pipelines"))
    monkeypatch.setenv("LLM_API_KEY", "offline-test-key")
    monkeypatch.setenv("EMBEDDING_API_KEY", "offline-test-key")
    monkeypatch.setenv("MOCK_EMBEDDING", "true")
    monkeypatch.setenv("RAISE_INCREMENTAL_LOADING_ERRORS", "true")

    def reset_engines():
        Container()[PipelineContext].deactivate()
        _create_graph_engine.cache_clear()
        _create_vector_engine.cache_clear()
        create_relational_engine.cache_clear()
        get_dlt_destination.cache_clear()
        graph_db_config.set(None)
        vector_db_config.set(None)

    reset_engines()
    cognee.config.data_root_directory(str(tmp_path / "data"))
    cognee.config.system_root_directory(str(tmp_path / "system"))
    from cognee.base_config import get_base_config

    monkeypatch.setattr(get_base_config(), "cache_root_directory", str(tmp_path / "cache"))
    monkeypatch.setattr(importlib.import_module("cognee.shared.cache"), "_cache_manager", None)
    cognee.config.set_relational_db_config(
        {"db_provider": "sqlite", "db_path": str(tmp_path / "databases")}
    )
    cognee.config.set_migration_db_config({"migration_db_provider": "sqlite"})
    cognee.config.set_graph_db_config(
        {
            "graph_database_provider": "ladybug",
            "graph_dataset_database_handler": "ladybug",
            "graph_file_path": str(tmp_path / "system" / "databases" / "graph"),
            "graph_database_subprocess_enabled": False,
        }
    )
    cognee.config.set_vector_db_config(
        {
            "vector_db_provider": "lancedb",
            "vector_dataset_database_handler": "lancedb",
            "vector_db_url": str(tmp_path / "system" / "databases" / "vectors"),
            "vector_db_subprocess_enabled": False,
        }
    )
    cognee.config.set_llm_config(
        {"llm_provider": "openai", "llm_model": "gpt-4o-mini", "llm_api_key": "offline-test-key"}
    )
    cognee.config.set_embedding_config(
        {
            "embedding_provider": "openai",
            "embedding_model": "openai/text-embedding-3-small",
            "embedding_dimensions": 384,
            "embedding_api_key": "offline-test-key",
        }
    )
    monkeypatch.setattr(LiteLLMEmbeddingEngine, "embed_text", _offline_embeddings)
    monkeypatch.setattr(LLMGateway, "acreate_structured_output", _offline_llm)
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    await engine_setup()
    yield tmp_path
    try:
        await cognee.prune.prune_data()
        await cognee.prune.prune_system(metadata=True)
    finally:
        reset_engines()


def _catalog():
    fake = FakeContentful()
    fake.models = [fake.model()]
    fake.initial_items = [fake.entry("alpha", "CATALOGALPHA"), fake.entry("beta", "CATALOGBETA")]
    return fake


async def _remember(
    fake, *, dataset=DATASET, source_id="default", remember_options=None, **selection
):
    source = contentful_source(
        fake.space,
        token="offline-contentful-token",
        environment=fake.environment,
        host=fake.host,
        source_id=source_id,
        client=fake.client,
        **selection,
    )
    return await cognee.remember(
        source,
        dataset_name=dataset,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
        run_in_background=False,
        self_improvement=False,
        **(remember_options or {}),
    )


async def _dataset(dataset_name=DATASET):
    user = await get_default_user()
    found = await get_authorized_existing_datasets(
        user=user, permission_type="read", datasets=[dataset_name]
    )
    assert len(found) == 1
    return found[0]


async def _records(dataset_name=DATASET):
    records = await get_dataset_data((await _dataset(dataset_name)).id)
    return [record for record in records if record.system_metadata.get("source") == "contentful"]


def _id_suffix(record):
    return record.system_metadata["external_id"].rsplit(":", 1)[-1]


async def _snapshot(dataset_name=DATASET):
    dataset = await _dataset(dataset_name)
    async with set_database_global_context_variables(dataset.id, dataset.owner_id):
        graph = await get_graph_engine()
        nodes, edges = await graph.get_graph_data()
        vector = await get_vector_engine_async()
        connection = await vector.get_connection()
        collections = {}
        for name in await connection.table_names():
            table = await vector.get_collection(name)
            collections[name] = [
                {key: value for key, value in row.items() if key != "vector"}
                for row in (await table.to_arrow()).to_pylist()
            ]
    return nodes, edges, collections


async def _assert_present(*markers, dataset=DATASET):
    nodes, _, vectors = await _snapshot(dataset)
    graph_text = json.dumps(nodes, default=str)
    vector_text = json.dumps(vectors, default=str)
    assert nodes and vectors, "remember must populate both real stores"
    for marker in markers:
        assert marker in graph_text
        assert marker in vector_text


async def _assert_absent(*markers, dataset=DATASET):
    records = await _records(dataset)
    relational_text = "\n".join(
        Path(unquote(urlsplit(record.raw_data_location).path)).read_text() for record in records
    )
    nodes, edges, vectors = await _snapshot(dataset)
    for marker in markers:
        assert marker not in relational_text
        assert marker not in json.dumps((nodes, edges), default=str)
        assert marker not in json.dumps(vectors, default=str)


async def _search_text(query, dataset=DATASET):
    results = await cognee.search(
        query_text=query, query_type=SearchType.CHUNKS, datasets=[dataset], top_k=100
    )
    return json.dumps(results, default=str)


async def _assert_indexed_document_ids(dataset=DATASET):
    """No vector or graph chunk may outlive its owning Cognee Data record."""
    expected = {str(record.id) for record in await _records(dataset)}
    nodes, _, vectors = await _snapshot(dataset)
    assert {
        str(properties["document_id"]) for _, properties in nodes if properties.get("document_id")
    } == expected
    assert {
        str(row["payload"]["document_id"]) for row in vectors.get("DocumentChunk_text", [])
    } == expected


def _sync_requests(fake):
    return [request for request in fake.requests if request.url.path.endswith("/sync")]


def _staged_documents(root):
    """Read the real SQL staging table, without creating a parallel pipeline."""
    rows = []
    for database in (root / "databases").glob("dlt_database_*"):
        # dlt's transient merge input still contains deletion tombstones. Only
        # the committed destination table is reconciled into Cognee.
        if not database.is_file() or database.name.endswith("_staging"):
            continue
        with sqlite3.connect(database) as connection:
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            for (table,) in tables:
                if table.startswith("contentful_documents_"):
                    rows.extend(connection.execute(f'SELECT id, content FROM "{table}"').fetchall())
    return rows


@pytest.mark.asyncio
async def test_lifecycle_updates_search_deletion_and_shared_entities(local_stores):
    fake = _catalog()
    await _remember(fake)
    first = {_id_suffix(record): record for record in await _records()}
    assert set(first) == {"alpha", "beta", "article"}
    assert all(
        record.system_metadata["table_name"].startswith("contentful_documents_")
        for record in first.values()
    )
    await _assert_present("CATALOGALPHA", "CATALOGBETA", "SharedCatalog")
    await _assert_indexed_document_ids()
    assert "CATALOGBETA" in await _search_text("CATALOGBETA")

    await _remember(fake)
    assert {_id_suffix(record): record.id for record in await _records()} == {
        key: record.id for key, record in first.items()
    }

    fake.delta("t1", [fake.entry("alpha", "CATALOGUPDATED", revision=2)], next_token="t2")
    await _remember(fake)
    updated = {_id_suffix(record): record for record in await _records()}
    assert updated["alpha"].id != first["alpha"].id
    assert updated["beta"].id == first["beta"].id
    await _assert_present("CATALOGUPDATED", "CATALOGBETA")
    await _assert_absent("CATALOGALPHA")
    await _assert_indexed_document_ids()

    fake.delta("t2", [fake.deleted("beta")], next_token="t3")
    await _remember(fake)
    assert {_id_suffix(record) for record in await _records()} == {"alpha", "article"}
    await _assert_absent("CATALOGBETA")
    await _assert_indexed_document_ids()
    await _assert_present("SharedCatalog", "CATALOGUPDATED")
    assert "CATALOGBETA" not in await _search_text("CATALOGBETA")

    fake.delta("t3", [fake.deleted("alpha")], next_token="t4")
    fake.models = []
    await _remember(fake)
    assert await _records() == [], "the final staging row must be forgotten as well"
    await _assert_absent("CATALOGUPDATED", "SharedCatalog")
    assert all(not rows for rows in (await _snapshot())[2].values())


@pytest.mark.asyncio
async def test_selection_refresh_isolated_by_source_and_dataset(local_stores):
    fake = _catalog()
    fake.models.append(fake.model("book", name="Book"))
    fake.initial_items.append(fake.entry("book-entry", "CATALOGBOOK", content_type="book"))
    fake.initial_items.append(fake.asset("image", "CATALOGIMAGE"))
    await _remember(fake, source_id="all")
    await _remember(fake, source_id="selected")
    await _remember(fake, dataset="independent_dataset", source_id="selected")
    before = await _records()
    tables = {record.system_metadata["table_name"] for record in before}
    assert len(tables) == 2
    other_ids = {record.id for record in await _records("independent_dataset")}
    assert not other_ids.intersection(record.id for record in before)

    await _remember(fake, source_id="selected", content_type_ids=["book"], include_assets=False)
    after = await _records()
    grouped = {
        table: {
            _id_suffix(record) for record in after if record.system_metadata["table_name"] == table
        }
        for table in tables
    }
    assert {frozenset(ids) for ids in grouped.values()} == {
        frozenset({"alpha", "beta", "article", "book-entry", "book", "image"}),
        frozenset({"book-entry", "book"}),
    }
    assert {record.id for record in await _records("independent_dataset")} == other_ids
    await _assert_indexed_document_ids()
    await _assert_indexed_document_ids("independent_dataset")
    await _assert_present("CATALOGALPHA", "CATALOGIMAGE")
    await _assert_present("CATALOGIMAGE", dataset="independent_dataset")

    await _remember(fake, source_id="selected")
    assert len(await _records()) == len(before)
    assert len({record.id for record in await _records()}) == len(before)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["adapter", "storage"])
async def test_loaded_update_and_deletion_recover_after_ingestion_failure(
    local_stores, monkeypatch, failure_stage
):
    fake = _catalog()
    await _remember(fake)
    initial_ids = {record.id for record in await _records()}
    fake.delta(
        "t1",
        [fake.entry("alpha", "CATALOGRECOVERED", revision=2), fake.deleted("beta")],
        next_token="t2",
    )
    module = importlib.import_module(
        "cognee.tasks.ingestion.resolve_dlt_sources"
        if failure_stage == "adapter"
        else "cognee.tasks.ingestion.ingest_data"
    )
    method = "_build_document_data_item" if failure_stage == "adapter" else "data_item_to_text_file"
    real_ingest = getattr(module, method)

    def fail_after_staging(*args, **kwargs):
        raise RuntimeError("injected failure after successful dlt load")

    async def fail_storage(*args, **kwargs):
        raise RuntimeError("injected failure after successful dlt load")

    monkeypatch.setattr(
        module, method, fail_after_staging if failure_stage == "adapter" else fail_storage
    )
    with pytest.raises(RuntimeError, match="after successful dlt load"):
        await _remember(fake)
    assert {record.id for record in await _records()} == initial_ids
    staging = _staged_documents(local_stores)
    assert any("CATALOGRECOVERED" in content for _, content in staging)
    assert all(not row_id.endswith(":beta") for row_id, _ in staging)

    monkeypatch.setattr(module, method, real_ingest)
    fake.requests.clear()
    await _remember(fake)
    assert _sync_requests(fake)[0].url.params["sync_token"] == "t2", (
        "retry must use an empty provider delta"
    )
    assert {_id_suffix(record) for record in await _records()} == {"alpha", "article"}
    await _assert_present("CATALOGRECOVERED")
    await _assert_absent("CATALOGALPHA", "CATALOGBETA")


@pytest.mark.asyncio
async def test_loaded_delta_recovers_after_partial_cognee_commit(local_stores, monkeypatch):
    fake = _catalog()
    await _remember(fake)
    initial = {_id_suffix(record): record for record in await _records()}
    fake.delta(
        "t1",
        [
            fake.entry("alpha", "CATALOGRECOVERED", revision=2),
            fake.entry("gamma", "CATALOGNEW"),
            fake.deleted("beta"),
        ],
        next_token="t2",
    )
    ingestion = importlib.import_module("cognee.tasks.ingestion.ingest_data")
    real_loader = ingestion.data_item_to_text_file

    async def fail_gamma(file_path, *args, **kwargs):
        text = Path(unquote(urlsplit(str(file_path)).path)).read_text()
        if "CATALOGNEW" in text:
            raise RuntimeError("injected failure after partial Cognee commit")
        return await real_loader(file_path, *args, **kwargs)

    monkeypatch.setattr(ingestion, "data_item_to_text_file", fail_gamma)
    with pytest.raises(RuntimeError, match="partial Cognee commit"):
        # Serial item processing makes the observed partial commit deterministic.
        await _remember(fake, remember_options={"data_per_batch": 1})
    partial = await _records()
    assert any(
        _id_suffix(record) == "alpha" and record.id != initial["alpha"].id for record in partial
    ), "the replacement must already be committed before the later item fails"
    assert initial["beta"].id in {record.id for record in partial}
    assert all(_id_suffix(record) != "gamma" for record in partial)

    monkeypatch.setattr(ingestion, "data_item_to_text_file", real_loader)
    fake.requests.clear()
    await _remember(fake)
    assert _sync_requests(fake)[0].url.params["sync_token"] == "t2"
    assert {_id_suffix(record) for record in await _records()} == {"alpha", "gamma", "article"}
    await _assert_present("CATALOGRECOVERED", "CATALOGNEW")
    await _assert_absent("CATALOGALPHA", "CATALOGBETA")
    await _assert_indexed_document_ids()


@pytest.mark.asyncio
async def test_unchanged_delta_retries_failed_cognification(local_stores, monkeypatch):
    fake = _catalog()

    async def fail_extraction(text_input, system_prompt, response_model, **kwargs):
        if response_model is KnowledgeGraph:
            raise RuntimeError("injected cognification failure")
        return await _offline_llm(text_input, system_prompt, response_model, **kwargs)

    monkeypatch.setattr(LLMGateway, "acreate_structured_output", fail_extraction)
    with pytest.raises(Exception, match="injected cognification failure"):
        await _remember(fake)
    assert {_id_suffix(record) for record in await _records()} == {"alpha", "beta", "article"}
    nodes, _, vectors = await _snapshot()
    assert "CATALOGALPHA" not in json.dumps(nodes, default=str)
    assert "CATALOGALPHA" not in json.dumps(vectors, default=str)

    monkeypatch.setattr(LLMGateway, "acreate_structured_output", _offline_llm)
    fake.requests.clear()
    await _remember(fake)
    assert _sync_requests(fake)[0].url.params["sync_token"] == "t1"
    await _assert_present("CATALOGALPHA", "CATALOGBETA")


@pytest.mark.asyncio
async def test_unchanged_delta_retries_logged_individual_cleanup_failure(
    local_stores, monkeypatch, caplog
):
    fake = _catalog()
    await _remember(fake)
    beta = next(record for record in await _records() if _id_suffix(record) == "beta")
    cleanup = importlib.import_module("cognee.modules.graph.methods.delete_data_nodes_and_edges")
    real_delete = cleanup.delete_data_nodes_and_edges
    attempted = []

    async def fail_beta(dataset_id, data_id, user_id):
        if data_id == beta.id:
            attempted.append(data_id)
            raise RuntimeError("injected individual cleanup failure")
        return await real_delete(dataset_id, data_id, user_id)

    monkeypatch.setattr(cleanup, "delete_data_nodes_and_edges", fail_beta)
    fake.delta("t1", [fake.deleted("beta")], next_token="t2")
    await _remember(fake)  # Cognee logs this individual failure without raising.
    assert attempted == [beta.id]
    assert "Failed to delete" in caplog.text
    assert "beta" in {_id_suffix(record) for record in await _records()}
    await _assert_present("CATALOGBETA")

    monkeypatch.setattr(cleanup, "delete_data_nodes_and_edges", real_delete)
    fake.requests.clear()
    await _remember(fake)
    assert _sync_requests(fake)[0].url.params["sync_token"] == "t2"
    assert "beta" not in {_id_suffix(record) for record in await _records()}
    await _assert_absent("CATALOGBETA")
    await _assert_present("CATALOGALPHA", "SharedCatalog")
