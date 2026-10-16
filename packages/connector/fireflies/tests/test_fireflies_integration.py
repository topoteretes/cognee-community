"""Real dlt + SQLite + Cognee graph/vector integration, with external APIs mocked.

The LLM fixture deliberately returns no entities or relationships. Speakers
must still be stored, searchable, updated and forgotten by the connector.
"""

import sys

import cognee
import pytest
import pytest_asyncio
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.infrastructure.databases.vector import get_vector_engine_async
from cognee.infrastructure.databases.vector.embeddings.LiteLLMEmbeddingEngine import (
    LiteLLMEmbeddingEngine,
)
from cognee.infrastructure.llm import LLMGateway

from cognee_community_connector_fireflies import fireflies_source, sync_fireflies

DATASET = "fireflies_integration"


class MeetingClient:
    def __init__(self):
        self.meetings = {
            "a": ("AliceBlue", "ship the cobalt feature"),
            "b": ("BobGreen", "review the emerald report"),
        }

    def list_transcripts(self, *, limit, skip):
        return [{"id": key, "date": 1000} for key in self.meetings][skip : skip + limit]

    def get_transcript(self, transcript_id, config):
        name, task = self.meetings[transcript_id]
        return {
            "id": transcript_id,
            "date": 1000,
            "title": f"Meeting {transcript_id}",
            "speakers": [{"id": 1, "name": name}],
            "sentences": [
                {
                    "speaker_id": 1,
                    "speaker_name": name,
                    "text": f"I will {task}.",
                    "start_time": 0,
                    "end_time": 5,
                }
            ],
            "summary": {
                "overview": f"{name} discussed the project.",
                "action_items": f"{name} will {task}.",
            },
        }


@pytest_asyncio.fixture
async def environment(tmp_path_factory, monkeypatch):
    import dlt

    # Short paths avoid Windows MAX_PATH failures in Lance transaction files.
    tmp_path = tmp_path_factory.mktemp("f")
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt"))
    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
    # Avoid Windows follow-up SQL temporary-file races with concurrent loaders.
    monkeypatch.setenv("LOAD__WORKERS", "1")
    cognee.config.data_root_directory(str(tmp_path / "d"))
    cognee.config.system_root_directory(str(tmp_path / "s"))
    cognee.config.set_relational_db_config({"db_provider": "sqlite"})
    from cognee.tasks.ingestion.get_dlt_destination import get_dlt_destination

    # The destination cache is keyed by dataset name, not storage root. Each
    # isolated test uses a different root with the same dataset name.
    get_dlt_destination.cache_clear()
    inputs = []
    original_pipeline = dlt.pipeline

    def pipeline(*args, **kwargs):
        kwargs.setdefault("pipelines_dir", str(tmp_path / "p"))
        return original_pipeline(*args, **kwargs)

    monkeypatch.setattr(dlt, "pipeline", pipeline)
    if sys.platform == "win32":
        from cognee.infrastructure.databases.graph.kuzu.subprocess import proxy

        # Cognee 1.4.0's optional throwaway-DB warm-up can leave a locked
        # temporary directory on Windows. The real connection still performs
        # LOAD / INSTALL JSON; graph operations and cleanup remain unmocked.
        monkeypatch.setattr(proxy, "install_json_extension", lambda *_args: None)

    async def structured(text_input=None, system_prompt=None, response_model=str, **kwargs):
        from cognee.shared.data_models import KnowledgeGraph, SummarizedContent

        if response_model is str:
            return "A mocked answer."
        if response_model == SummarizedContent:
            return SummarizedContent(summary="Meeting summary", description="Meeting summary")
        if response_model == KnowledgeGraph:
            inputs.append(text_input)
            for name in ("AliceBlue", "BobGreen"):
                if name not in (text_input or ""):
                    continue
                if "## Transcript" in text_input:
                    assert f"{name} (speaker_id: 1) said:" in text_input
            return KnowledgeGraph(nodes=[], edges=[])
        return response_model()

    async def embed(self, texts):
        return [[0.1] * self.get_vector_size() for _ in texts]

    monkeypatch.setattr(LLMGateway, "acreate_structured_output", structured)
    monkeypatch.setattr(LiteLLMEmbeddingEngine, "embed_text", embed)
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    yield inputs
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)


async def graph_text():
    nodes, edges = await (await get_graph_engine()).get_graph_data()
    return str(nodes), str(edges)


async def source_records():
    from cognee.modules.data.methods.get_dataset_data import get_dataset_data
    from cognee.modules.pipelines.layers.resolve_authorized_user_dataset import (
        resolve_authorized_user_dataset,
    )

    _, dataset = await resolve_authorized_user_dataset(dataset_name=DATASET)
    return [
        item
        for item in await get_dataset_data(dataset.id)
        if (item.external_metadata or {}).get("source") == "fireflies"
    ]


async def speaker_entities():
    nodes, _ = await (await get_graph_engine()).get_graph_data()
    return {node_id: node for node_id, node in nodes if node.get("type") == "Entity"}


async def assert_speaker_links():
    graph = await get_graph_engine()
    nodes, edges = await graph.get_graph_data()
    person_ids = {
        i for i, n in nodes if n.get("type") == "EntityType" and n.get("name") == "Person"
    }
    documents = {
        str(item.id): item.external_metadata["external_id"] for item in await source_records()
    }
    for entity_id in await speaker_entities():
        links = [
            (target, props)
            for source, target, rel, props in edges
            if source == entity_id and rel == "spoke_in"
        ]
        assert len(links) == 1
        target, props = links[0]
        assert props["fireflies_transcript_id"] == documents[target]
        assert any(
            source == entity_id and target in person_ids and rel == "is_a"
            for source, target, rel, _ in edges
        )


async def speaker_vectors(ids):
    return await (await get_vector_engine_async()).retrieve("Entity_name", list(ids))


@pytest.mark.asyncio
async def test_speakers_search_updates_and_forget_including_last_meeting(environment):
    client = MeetingClient()
    await sync_fireflies(fireflies_source(client=client), dataset_name=DATASET)
    nodes, edges = await graph_text()
    assert "AliceBlue" in nodes and "BobGreen" in nodes
    assert "spoke_in" in edges
    assert {node["name"] for node in (await speaker_entities()).values()} == {
        "AliceBlue",
        "BobGreen",
    }
    assert len(await source_records()) == 2
    first_ids = {item.id for item in await source_records()}
    first_extractions = len(environment)
    first_speakers = set(await speaker_entities())
    await assert_speaker_links()
    assert len(await speaker_vectors(first_speakers)) == 2

    await sync_fireflies(fireflies_source(client=client), dataset_name=DATASET)
    assert {item.id for item in await source_records()} == first_ids
    assert len(environment) == first_extractions
    assert set(await speaker_entities()) == first_speakers

    original_listing = client.list_transcripts

    def broken(**kwargs):
        raise RuntimeError("upstream failure")

    client.list_transcripts = broken
    with pytest.raises(Exception, match="upstream failure"):
        await sync_fireflies(fireflies_source(client=client), dataset_name=DATASET)
    nodes, _ = await graph_text()
    assert "AliceBlue" in nodes and "BobGreen" in nodes
    assert len(await source_records()) == 2
    client.list_transcripts = original_listing

    results = await cognee.search(
        query_text="cobalt feature", query_type=cognee.SearchType.CHUNKS, datasets=[DATASET]
    )
    assert "cobalt" in str(results)

    client.meetings["a"] = ("AliceBlue", "ship the sapphire feature")
    await sync_fireflies(fireflies_source(client=client), dataset_name=DATASET)
    assert len(await source_records()) == 2
    results = await cognee.search(
        query_text="sapphire", query_type=cognee.SearchType.CHUNKS, datasets=[DATASET]
    )
    assert "sapphire" in str(results)
    assert "cobalt" not in str(results)

    del client.meetings["b"]
    await sync_fireflies(fireflies_source(client=client), dataset_name=DATASET)
    nodes, _ = await graph_text()
    assert "AliceBlue" in nodes and "BobGreen" not in nodes
    assert len(await source_records()) == 1
    remaining_speakers = set(await speaker_entities())
    assert await speaker_vectors(first_speakers - remaining_speakers) == []
    assert len(await speaker_vectors(remaining_speakers)) == 1

    client.meetings.clear()
    await sync_fireflies(fireflies_source(client=client), dataset_name=DATASET)
    nodes, _ = await graph_text()
    assert "AliceBlue" not in nodes and "BobGreen" not in nodes
    assert await source_records() == []
    assert await speaker_vectors(first_speakers) == []
    results = await cognee.search(
        query_text="sapphire", query_type=cognee.SearchType.CHUNKS, datasets=[DATASET]
    )
    assert "sapphire" not in str(results)


@pytest.mark.asyncio
async def test_speaker_only_scope_rename_and_selection(environment, monkeypatch):
    client = MeetingClient()
    client.meetings = {"a": ("Speaker 1", "one"), "b": ("Speaker 1", "two")}
    options = {"include_transcript": False, "include_summary": False, "include_action_items": False}
    await sync_fireflies(fireflies_source(client=client, **options), dataset_name=DATASET)
    entities = await speaker_entities()
    assert {node["name"] for node in entities.values()} == {
        "Speaker 1 in meeting a",
        "Speaker 1 in meeting b",
    }
    first_ids = set(entities)
    await assert_speaker_links()
    extractions = len(environment)

    async def unexpected_embedding(*_args, **_kwargs):
        raise AssertionError("Unchanged speaker sync must not repeat embeddings")

    with monkeypatch.context() as unchanged:
        unchanged.setattr(LiteLLMEmbeddingEngine, "embed_text", unexpected_embedding)
        await sync_fireflies(fireflies_source(client=client, **options), dataset_name=DATASET)
    assert len(environment) == extractions
    assert set(await speaker_entities()) == first_ids

    client.meetings["a"] = ("AliceActual", "one")
    await sync_fireflies(fireflies_source(client=client, **options), dataset_name=DATASET)
    entities = await speaker_entities()
    assert set(entities) == first_ids
    assert {node["name"] for node in entities.values()} == {"AliceActual", "Speaker 1 in meeting b"}

    # Switching off speakers replaces documents and removes old speaker artifacts.
    await sync_fireflies(
        fireflies_source(client=client, include_speakers=False), dataset_name=DATASET
    )
    assert await speaker_entities() == {}


@pytest.mark.asyncio
async def test_failed_speaker_write_retries_without_repeating_llm(environment, monkeypatch):
    import cognee.tasks.storage as storage

    client = MeetingClient()
    original = storage.add_data_points

    async def partial_failure(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("speaker vector write failed")

    with monkeypatch.context() as failure:
        failure.setattr(storage, "add_data_points", partial_failure)
        with pytest.raises(RuntimeError, match="speaker vector write failed"):
            await sync_fireflies(fireflies_source(client=client), dataset_name=DATASET)
    assert all(
        "fireflies_speaker_sync" not in item.external_metadata for item in await source_records()
    )
    extractions = len(environment)
    await sync_fireflies(fireflies_source(client=client), dataset_name=DATASET)
    assert len(environment) == extractions
    assert {node["name"] for node in (await speaker_entities()).values()} == {
        "AliceBlue",
        "BobGreen",
    }
    assert all(
        item.external_metadata.get("fireflies_speaker_sync") for item in await source_records()
    )
