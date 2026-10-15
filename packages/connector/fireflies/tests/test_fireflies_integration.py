"""Real dlt + SQLite + Cognee graph/vector integration, with external APIs mocked.

The LLM fixture verifies the extraction input and returns a deterministic graph.
This proves document routing and persistence/cleanup, not a live LLM's accuracy.
"""

import sys

import cognee
import pytest
import pytest_asyncio
from cognee.infrastructure.databases.graph import get_graph_engine
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
        from cognee.shared.data_models import Edge, KnowledgeGraph, Node, SummarizedContent

        if response_model is str:
            return "A mocked answer."
        if response_model == SummarizedContent:
            return SummarizedContent(summary="Meeting summary", description="Meeting summary")
        if response_model == KnowledgeGraph:
            inputs.append(text_input)
            nodes, edges = [], []
            for name, meeting in (("AliceBlue", "a"), ("BobGreen", "b")):
                if name not in (text_input or ""):
                    continue
                assert f"{name} (speaker_id: 1) said:" in text_input
                nodes.extend(
                    [
                        Node(id=name, name=name, type="Person", description=f"{name} is a speaker"),
                        Node(
                            id=f"meeting-{meeting}",
                            name=f"Meeting {meeting}",
                            type="Meeting",
                            description=f"Meeting with {name}",
                        ),
                    ]
                )
                edges.append(
                    Edge(
                        source_node_id=name,
                        target_node_id=f"meeting-{meeting}",
                        relationship_name="spoke_in",
                    )
                )
            return KnowledgeGraph(nodes=nodes, edges=edges)
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


@pytest.mark.asyncio
async def test_speakers_search_updates_and_forget_including_last_meeting(environment):
    client = MeetingClient()
    await sync_fireflies(fireflies_source(client=client), dataset_name=DATASET)
    nodes, edges = await graph_text()
    assert "AliceBlue" in nodes and "BobGreen" in nodes
    assert "spoke_in" in edges
    assert len(await source_records()) == 2
    first_ids = {item.id for item in await source_records()}
    first_extractions = len(environment)

    await sync_fireflies(fireflies_source(client=client), dataset_name=DATASET)
    assert {item.id for item in await source_records()} == first_ids
    assert len(environment) == first_extractions

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

    client.meetings.clear()
    await sync_fireflies(fireflies_source(client=client), dataset_name=DATASET)
    nodes, _ = await graph_text()
    assert "AliceBlue" not in nodes and "BobGreen" not in nodes
    assert await source_records() == []
    results = await cognee.search(
        query_text="sapphire", query_type=cognee.SearchType.CHUNKS, datasets=[DATASET]
    )
    assert "sapphire" not in str(results)
