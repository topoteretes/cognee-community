"""Real Cognee graph/vector/relational sync; model output is deterministic.

Only the upstream API and model calls are doubled. dlt, document routing,
Cognee ingestion, retrieval, database ownership and deletion are real.
"""

from hashlib import sha256
from pathlib import Path
from runpy import run_path
from unittest.mock import patch

import cognee
import pytest
from cognee.context_global_variables import (
    graph_db_config,
    set_database_global_context_variables,
    vector_db_config,
)
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.engine.operations.setup import setup as engine_setup
from cognee.modules.users.methods import get_default_user
from conftest import FakeElasticsearch, document

from cognee_community_connector_elasticsearch import elasticsearch_source

pytestmark = [pytest.mark.cognee, pytest.mark.asyncio(loop_scope="module")]
DATASET = "elasticsearch_connector_test"


@pytest.fixture(scope="module")
async def local_memory(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("cognee_memory")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
    monkeypatch.setenv("ENABLE_BACKEND_ACCESS_CONTROL", "true")
    monkeypatch.setenv("GRAPH_DATASET_DATABASE_HANDLER", "ladybug")
    monkeypatch.setenv("VECTOR_DATASET_DATABASE_HANDLER", "lancedb")
    monkeypatch.setenv("LLM_API_KEY", "sk-local-test-placeholder")
    monkeypatch.setenv("MOCK_EMBEDDING", "true")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("EMBEDDING_MODEL", "openai/text-embedding-3-large")
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "384")
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt"))
    from cognee.infrastructure.databases.graph.get_graph_engine import _create_graph_engine
    from cognee.infrastructure.databases.relational.create_relational_engine import (
        create_relational_engine,
    )
    from cognee.infrastructure.databases.vector.create_vector_engine import _create_vector_engine

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
    cognee.config.set_vector_db_url(str(tmp_path / "system" / "databases" / "cognee.lancedb"))
    await engine_setup()
    from cognee.shared.data_models import KnowledgeGraph, Node, SummarizedContent

    async def output(text_input, system_prompt, response_model, **kwargs):
        if response_model.__name__ == "KnowledgeGraph":
            identity = sha256(text_input.encode()).hexdigest()[:12]
            return KnowledgeGraph(
                nodes=[
                    Node(
                        id=identity,
                        name=identity,
                        type="Concept",
                        description="Test document concept",
                    )
                ],
                edges=[],
            )
        if response_model.__name__ == "SummarizedContent":
            return SummarizedContent(summary=text_input[:120], description="Test summary")
        return response_model()

    from cognee.infrastructure.databases.vector.embeddings.LiteLLMEmbeddingEngine import (
        LiteLLMEmbeddingEngine,
    )

    async def embed(self, texts):
        return [[1.0 / self.dimensions**0.5] * self.dimensions for text in texts]

    monkeypatch.setattr(LiteLLMEmbeddingEngine, "embed_text", embed)

    with patch(
        "cognee.infrastructure.llm.LLMGateway.LLMGateway.acreate_structured_output",
        side_effect=output,
    ):
        yield
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    monkeypatch.undo()


async def snapshot(dataset_name=DATASET):
    from cognee.infrastructure.databases.graph import get_graph_engine
    from cognee.infrastructure.databases.vector import get_vector_engine_async

    user = await get_default_user()
    dataset = (
        await get_authorized_existing_datasets(
            user=user, permission_type="read", datasets=[dataset_name]
        )
    )[0]
    records = await get_dataset_data(dataset.id)
    async with set_database_global_context_variables(dataset.id, dataset.owner_id):
        nodes, _ = await (await get_graph_engine()).get_graph_data()
        vector = await get_vector_engine_async()
        collection = await vector.get_collection("DocumentChunk_text")
        count = await collection.count_rows()
    text = "\n".join(str(properties.get("text", "")) for _, properties in nodes)
    return records, text, count


async def remember(client, dataset_name=DATASET, **kwargs):
    source = elasticsearch_source(
        source_id="test", index="articles", client=client, fields=["title", "body"], **kwargs
    )
    return await cognee.remember(
        source,
        dataset_name=dataset_name,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
        self_improvement=False,
    )


async def test_document_search_updates_and_final_delete(local_memory):
    client = FakeElasticsearch(
        [
            document("1", body="Alpha runbook restart service"),
            document("2", body="Beta onboarding request VPN"),
        ]
    )
    await remember(client)
    records, text, count = await snapshot()
    assert len(records) == count == 2
    assert all(record.system_metadata["source"] == "elasticsearch" for record in records)
    assert "Alpha runbook" in text and "Beta onboarding" in text
    from cognee.api.v1.search import SearchType

    results = await cognee.search("onboarding", query_type=SearchType.CHUNKS, datasets=[DATASET])
    assert results
    assert "Beta onboarding" in str(results)
    ids = {record.id for record in records}
    await remember(client)
    records, _, count = await snapshot()
    assert {record.id for record in records} == ids and count == 2
    client.documents = [document("1", seq=2, date=2000, body="Gamma revised runbook")]
    await remember(client)
    records, text, count = await snapshot()
    assert len(records) == count == 1
    assert "Gamma revised" in text
    assert "Alpha runbook" not in text and "Beta onboarding" not in text
    client.documents = []
    await remember(client)
    records, text, count = await snapshot()
    assert records == [] and count == 0
    assert "Gamma revised" not in text
    results = await cognee.search("onboarding", query_type=SearchType.CHUNKS, datasets=[DATASET])
    assert all(not result["search_result"] for result in results)


async def test_alternating_datasets_and_query_scopes(local_memory):
    client = FakeElasticsearch(
        [document("1", body="One scope"), document("2", body="Other scope", published=False)]
    )
    await remember(client, query={"term": {"published": True}})
    await remember(client, dataset_name="second_dataset", query={"term": {"published": True}})
    await remember(client, query={"term": {"published": False}})
    records, _, count = await snapshot()
    assert len(records) == count == 2
    client.documents = [client.documents[1]]
    await remember(client, query={"term": {"published": True}})
    records, text, count = await snapshot()
    assert len(records) == count == 1 and "Other scope" in text
    records, text, count = await snapshot("second_dataset")
    assert len(records) == count == 1 and "One scope" in text
    await remember(client, dataset_name="second_dataset", query={"term": {"published": True}})
    records, _, count = await snapshot("second_dataset")
    assert not records and count == 0


async def test_upstream_failure_keeps_all_existing_memory(local_memory):
    dataset = "es_failed_scan_memory"
    client = FakeElasticsearch([document(body="Must remain after a network failure")])
    await remember(client, dataset_name=dataset)
    before, text, count = await snapshot(dataset)
    assert len(before) == count == 1
    client.documents = []
    client.fail_at = len(client.requests) + 1
    with pytest.raises(Exception, match="simulated connection failure"):
        await remember(client, dataset_name=dataset)
    after, text, count = await snapshot(dataset)
    assert {record.id for record in after} == {record.id for record in before}
    assert count == 1 and "Must remain" in text
    client.fail_at = None
    await remember(client, dataset_name=dataset)
    records, _, count = await snapshot(dataset)
    assert records == [] and count == 0


async def test_failed_cognify_replays_staging_without_upstream_edit(local_memory):
    dataset = "es_cognify_recovery"
    client = FakeElasticsearch([document(body="Previous version")])
    await remember(client, dataset_name=dataset)
    client.documents = [document(seq=2, body="Replayed version")]
    with (
        patch(
            "cognee.infrastructure.llm.LLMGateway.LLMGateway.acreate_structured_output",
            side_effect=RuntimeError("synthetic model outage"),
        ),
        pytest.raises(Exception, match="synthetic model outage"),
    ):
        await remember(client, dataset_name=dataset)
    # The connector cursor is already staged. A zero-delta sync must retry the
    # failed Cognee work from staging, without requiring another upstream edit.
    client.requests.clear()
    await remember(client, dataset_name=dataset)
    records, text, count = await snapshot(dataset)
    assert len(records) == count == 1 and "Replayed version" in text
    assert "Previous version" not in text
    assert all(request["source"] is False for request in client.requests)


async def test_failed_graph_cleanup_is_retried_on_zero_delta_sync(local_memory):
    from importlib import import_module
    from unittest.mock import AsyncMock

    dataset = "es_cleanup_recovery"
    client = FakeElasticsearch(
        [document("keep", body="Keep this memory"), document("remove", body="Remove this memory")]
    )
    await remember(client, dataset_name=dataset)
    client.documents = [client.documents[0]]
    deletion = import_module("cognee.modules.graph.methods.delete_data_nodes_and_edges")
    with patch.object(
        deletion,
        "delete_data_nodes_and_edges",
        new=AsyncMock(side_effect=RuntimeError("synthetic graph outage")),
    ):
        await remember(client, dataset_name=dataset)
    # Cognee logs a failed cleanup; the old record remains until retry. The
    # connector must still reconcile retained staging rows on a zero delta.
    records, _, count = await snapshot(dataset)
    assert len(records) == count == 2
    client.requests.clear()
    await remember(client, dataset_name=dataset)
    records, text, count = await snapshot(dataset)
    assert len(records) == count == 1
    assert "Keep this memory" in text and "Remove this memory" not in text
    assert all(request["source"] is False for request in client.requests)


@pytest.mark.live
async def test_runnable_example_with_live_source_updates_and_deletion(
    local_memory, live, monkeypatch, capsys
):
    """Run the shipped example on real ES/Cognee; force deterministic chunk recall."""
    import os

    from cognee.api.v1.search import SearchType

    admin, _, index, key = live
    dataset = "es_live_example"
    monkeypatch.setenv("ELASTICSEARCH_URL", os.environ["ES_TEST_URL"])
    monkeypatch.setenv("ELASTICSEARCH_API_KEY", key)
    monkeypatch.setenv("ELASTICSEARCH_INDEX", index)
    monkeypatch.setenv("ELASTICSEARCH_SOURCE_ID", "live-example-reader")
    monkeypatch.setenv("COGNEE_DATASET", dataset)
    monkeypatch.setenv("COGNEE_QUESTION", "onboarding")
    original_recall = cognee.recall

    async def chunk_recall(query_text, *, datasets):
        # Exercise the real recall API without claiming model answer quality.
        return await original_recall(query_text, datasets=datasets, query_type=SearchType.CHUNKS)

    monkeypatch.setattr(cognee, "recall", chunk_recall)
    example = run_path(str(Path(__file__).parents[1] / "examples" / "remember_elasticsearch.py"))
    admin.index(
        index=index,
        id="onboarding",
        document={
            "title": "Team onboarding",
            "body": "Request VPN access through the helpdesk.",
            "updated_at": 1000,
        },
        refresh="wait_for",
    )
    await example["main"]()
    records, text, count = await snapshot(dataset)
    assert len(records) == count == 1 and "helpdesk" in text
    first_ids = {record.id for record in records}
    assert "helpdesk" in capsys.readouterr().out
    await example["main"]()
    records, _, count = await snapshot(dataset)
    assert {record.id for record in records} == first_ids and count == 1
    admin.update(
        index=index,
        id="onboarding",
        doc={"body": "Request VPN access through the identity portal.", "updated_at": 2000},
        refresh="wait_for",
    )
    await example["main"]()
    records, text, count = await snapshot(dataset)
    assert len(records) == count == 1 and "identity portal" in text and "helpdesk" not in text
    admin.delete(index=index, id="onboarding", refresh="wait_for")
    await example["main"]()
    records, text, count = await snapshot(dataset)
    assert records == [] and count == 0 and "identity portal" not in text
