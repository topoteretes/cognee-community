"""Offline remember/recall/forget integration with real local Cognee stores."""

import hashlib
import importlib
import os
from unittest.mock import patch

import cognee
import httpx
import pytest
from cognee_community_connector_twitch import twitch_source
from test_twitch import Helix

DATASET = "twitch_offline"


@pytest.fixture
async def local_cognee(tmp_path, monkeypatch):
    import tiktoken
    from cognee.context_global_variables import graph_db_config, vector_db_config
    from cognee.infrastructure.databases.graph.get_graph_engine import _create_graph_engine
    from cognee.infrastructure.databases.relational.create_relational_engine import (
        create_relational_engine,
    )
    from cognee.infrastructure.databases.vector.create_vector_engine import _create_vector_engine
    from cognee.modules.engine.operations.setup import setup
    from cognee.tasks.ingestion.get_dlt_destination import get_dlt_destination

    # Restricted environments can supply a preinstalled native JSON extension cache.
    extension_home = os.getenv("TWITCH_TEST_LADYBUG_HOME")
    if extension_home:
        from cognee.infrastructure.databases.graph.kuzu.subprocess.proxy import RemoteKuzuConnection

        original_load = RemoteKuzuConnection.load_extension

        def load_extension(connection, name):
            escaped = extension_home.replace("'", "''")
            connection.execute(f"CALL home_directory='{escaped}';")
            return original_load(connection, name)

        monkeypatch.setattr(RemoteKuzuConnection, "load_extension", load_extension)

    # Byte-level tokenizer avoids downloading tiktoken's vocabulary in offline tests.
    encoding = tiktoken.Encoding(
        name="offline",
        pat_str=r"(?s).",
        mergeable_ranks={bytes([i]): i for i in range(256)},
        special_tokens={},
    )
    monkeypatch.setattr(tiktoken, "encoding_for_model", lambda _: encoding)
    monkeypatch.setattr(tiktoken, "get_encoding", lambda _: encoding)
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
    factories = (
        _create_graph_engine,
        _create_vector_engine,
        create_relational_engine,
        get_dlt_destination,
    )
    for factory in factories:
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

    from cognee.shared.data_models import Edge, KnowledgeGraph, Node, SummarizedContent

    async def model(text_input, system_prompt, response_model, **kwargs):
        identity = hashlib.sha256((text_input or "").encode()).hexdigest()[:12]
        if response_model.__name__ == "KnowledgeGraph":
            return KnowledgeGraph(
                nodes=[
                    Node(id=identity, name=identity, type="Topic", description="stream metadata"),
                    Node(id="rust", name="Rust", type="Topic", description="programming language"),
                ],
                edges=[
                    Edge(source_node_id=identity, target_node_id="rust", relationship_name="covers")
                ],
            )
        if response_model.__name__ == "SummarizedContent":
            return SummarizedContent(summary=(text_input or "")[:120], description="")
        return response_model()

    async def embed(self, texts):
        from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import (
            EmbeddingResult,
        )

        return EmbeddingResult(vectors=[[1.0] + [0.0] * 383 for _ in texts], tokens=0)

    gateway = importlib.import_module("cognee.infrastructure.llm.LLMGateway")
    embedding = importlib.import_module(
        "cognee.infrastructure.databases.vector.embeddings.LiteLLMEmbeddingEngine"
    )
    with (
        patch.object(gateway.LLMGateway, "acreate_structured_output", side_effect=model),
        patch.object(
            embedding.LiteLLMEmbeddingEngine,
            "embed_text",
            embed,
        ),
    ):
        yield
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    for factory in factories:
        factory.cache_clear()


async def records():
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
        chunks = await (await vector.get_collection("DocumentChunk_text")).query().to_list()
    return nodes, edges, chunks


@pytest.mark.asyncio
async def test_remember_search_and_forget_final_channel(local_cognee):
    from cognee.modules.search.types import SearchType

    fake = Helix()

    async def sync():
        with httpx.Client(transport=httpx.MockTransport(fake.handler)) as client:
            await cognee.remember(
                twitch_source("secret", "app", http_client=client),
                dataset_name=DATASET,
                write_disposition="merge",
                primary_key="id",
                max_rows_per_table=0,
                self_improvement=False,
            )

    await sync()
    before, dataset = await records()
    assert {row.system_metadata["external_id"] for row in before} == {
        "channel:10",
        "video:v1",
        "clip:c1",
    }
    assert all(row.system_metadata["source"] == "twitch" for row in before)
    nodes, edges, chunks = await stores(dataset)
    assert nodes and edges and len(chunks) == 3
    result = await cognee.recall(
        "Rust", query_type=SearchType.CHUNKS, datasets=[DATASET], auto_route=False
    )
    assert "Rust ownership" in str(result)
    await sync()
    unchanged, _ = await records()
    assert {row.id for row in unchanged} == {row.id for row in before}
    fake.videos.clear()
    await sync()
    after_expiry, _ = await records()
    assert {row.system_metadata["external_id"] for row in after_expiry} == {"channel:10", "clip:c1"}
    assert "Rust ownership" not in str((await stores(dataset))[2])
    fake.follows.clear()
    await sync()
    assert (await records())[0] == []
    assert await stores(dataset) == ([], [], [])
