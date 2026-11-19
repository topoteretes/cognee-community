"""Real Cognee relational/graph/vector lifecycle; mocked LLM and embedding calls."""

import hashlib
import math
import re
from unittest.mock import patch

import pytest
from conftest import FakeClient

from cognee_community_connector_mixpanel import mixpanel_source, sync_mixpanel


@pytest.mark.integration
@pytest.mark.asyncio
async def test_graph_vector_final_delete_restore_and_no_change_replay(
    tmp_path, monkeypatch, report
):
    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
    monkeypatch.setenv("ENABLE_BACKEND_ACCESS_CONTROL", "true")
    monkeypatch.setenv("MOCK_EMBEDDING", "true")
    monkeypatch.setenv("LLM_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("EMBEDDING_MODEL", "openai/text-embedding-3-large")
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "384")

    import cognee
    from cognee.context_global_variables import (
        graph_db_config,
        set_database_global_context_variables,
        vector_db_config,
    )
    from cognee.infrastructure.databases.graph import get_graph_engine
    from cognee.infrastructure.databases.graph.get_graph_engine import _create_graph_engine
    from cognee.infrastructure.databases.relational.create_relational_engine import (
        create_relational_engine,
    )
    from cognee.infrastructure.databases.vector import get_vector_engine_async
    from cognee.infrastructure.databases.vector.create_vector_engine import _create_vector_engine
    from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import EmbeddingResult
    from cognee.modules.data.methods import get_authorized_existing_datasets
    from cognee.modules.data.methods.get_dataset_data import get_dataset_data
    from cognee.modules.engine.operations.setup import setup
    from cognee.modules.users.methods import get_default_user
    from cognee.shared.data_models import Edge, KnowledgeGraph, Node, SummarizedContent

    _create_graph_engine.cache_clear()
    _create_vector_engine.cache_clear()
    create_relational_engine.cache_clear()
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
    cognee.config.set_vector_db_url(str(tmp_path / "system/databases/cognee.lancedb"))
    await setup()

    async def llm(text_input, system_prompt, response_model, **kwargs):
        if response_model.__name__ == "KnowledgeGraph":
            digest = hashlib.sha256((text_input or "").encode()).hexdigest()[:8]
            return KnowledgeGraph(
                nodes=[
                    Node(
                        id=f"{digest}_a",
                        name=f"{digest}_a",
                        type="Concept",
                        description="synthetic",
                    ),
                    Node(
                        id=f"{digest}_b",
                        name=f"{digest}_b",
                        type="Concept",
                        description="synthetic",
                    ),
                ],
                edges=[
                    Edge(
                        source_node_id=f"{digest}_a",
                        target_node_id=f"{digest}_b",
                        relationship_name="defines",
                    )
                ],
            )
        if response_model.__name__ == "SummarizedContent":
            return SummarizedContent(summary=(text_input or "")[:120], description="")
        return response_model()

    client = FakeClient(tmp_path / "connector", report=report, scope="integration-synthetic")
    user = await get_default_user()

    async def records(dataset_name):
        ds = (
            await get_authorized_existing_datasets(
                user=user, permission_type="read", datasets=[dataset_name]
            )
        )[0]
        data = await get_dataset_data(ds.id)
        return ds, [d for d in data if d.system_metadata.get("source") == "mixpanel"]

    async def store_state(ds):
        async with set_database_global_context_variables(ds.id, ds.owner_id):
            graph = await get_graph_engine()
            nodes, _ = await graph.get_graph_data()
            vector = await get_vector_engine_async()
            chunks = await (await vector.get_collection("DocumentChunk_text")).count_rows()
            return nodes, chunks

    async def embed(engine, texts):
        # Built-in mock embeddings are zero vectors, undefined for cosine search.
        vectors = []
        for text in texts:
            vector = [0.0] * engine.dimensions
            for token in re.findall(r"\w+", text.lower()):
                index = int(hashlib.sha256(token.encode()).hexdigest()[:8], 16) % engine.dimensions
                vector[index] += 1.0
            norm = math.sqrt(sum(v * v for v in vector)) or 1
            vectors.append([v / norm for v in vector])
        return EmbeddingResult(vectors=vectors, tokens=0)

    with (
        patch(
            "cognee.infrastructure.llm.LLMGateway.LLMGateway.acreate_structured_output",
            side_effect=llm,
        ),
        patch(
            "cognee.infrastructure.databases.vector.embeddings."
            "LiteLLMEmbeddingEngine.LiteLLMEmbeddingEngine.embed_text",
            embed,
        ),
    ):
        dataset = await sync_mixpanel(mixpanel_source(client=client))
        ds, baseline = await records(dataset)
        assert len(baseline) == 1
        nodes, chunks = await store_state(ds)
        assert len(nodes) > 0 and chunks > 0
        results = await cognee.search(
            query_text="Signup", query_type=cognee.SearchType.CHUNKS, datasets=[dataset]
        )
        assert "Signup" in str(results)
        other_client = FakeClient(
            tmp_path / "other-connector", report=report, scope="another-project"
        )
        other_dataset = await sync_mixpanel(mixpanel_source(client=other_client))
        other_ds, other_records = await records(other_dataset)
        other_id = other_records[0].id
        assert other_dataset != dataset
        baseline_id = baseline[0].id
        await sync_mixpanel(mixpanel_source(client=client))
        assert (await records(dataset))[1][0].id == baseline_id

        client.data["report"][0]["name"] = "Updated definition"
        await sync_mixpanel(mixpanel_source(client=client))
        updated = (await records(dataset))[1]
        assert len(updated) == 1 and updated[0].id != baseline_id
        assert updated[0].system_metadata["title"].endswith("Updated definition")

        client.data["report"] = []
        await sync_mixpanel(mixpanel_source(client=client), cognify=False)
        assert (await records(dataset))[1] == []
        nodes_after, chunks_after = await store_state(ds)
        assert chunks_after == 0
        assert len(nodes_after) < len(nodes)
        assert (await records(other_dataset))[1][0].id == other_id
        assert (await store_state(other_ds))[1] > 0
        assert "Signup" not in str(
            await cognee.search(
                query_text="Signup", query_type=cognee.SearchType.CHUNKS, datasets=[dataset]
            )
        )

        client.data["report"] = [report]
        # A failure after staging/add must be recoverable with unchanged upstream.
        with patch.object(
            cognee, "cognify", side_effect=RuntimeError("synthetic downstream failure")
        ):
            with pytest.raises(RuntimeError, match="downstream failure"):
                await sync_mixpanel(mixpanel_source(client=client))
        await sync_mixpanel(mixpanel_source(client=client))
        assert len((await records(dataset))[1]) == 1
        assert (await store_state(ds))[1] > 0
