"""End-to-end through cognee: a page deleted upstream leaves the graph.

Syncs a fake wiki with ``cognee.add`` + ``cognify`` (LLM and embeddings
mocked, no credentials), deletes one page in the wiki, syncs again, and checks
that the deleted page's extracted entity is gone from the graph while the
surviving page's entity remains. It also checks the documents land as
``mediawiki`` documents under the wiki's node set, and that an unchanged
re-sync does not re-ingest anything.
"""

import importlib
import importlib.util
import pathlib

import cognee
import pytest
import pytest_asyncio
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.infrastructure.databases.vector.embeddings.LiteLLMEmbeddingEngine import (
    LiteLLMEmbeddingEngine,
)
from cognee.infrastructure.llm import LLMGateway
from fake_wiki import API_URL, FakeWiki

from cognee_community_connector_mediawiki import mediawiki_source

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")
_REAL_INDEXING = {
    name: getattr(add_data_points_module, name)
    for name in ("index_data_points", "index_graph_edges")
}

DATASET = "mediawiki_forget_test"
# Distinctive tokens so each page maps to exactly one graph entity.
ALPHA = "Alphacorp"
BRAVO = "Bravocorp"


async def _mock_structured_output(
    text_input=None, system_prompt=None, response_model=str, **_kwargs
):
    """Extract one entity named after whichever token appears in the chunk."""
    from cognee.shared.data_models import KnowledgeGraph, SummarizedContent
    from cognee.shared.data_models import Node as KGNode

    if response_model is str:
        return "Mocked answer."
    if response_model == SummarizedContent:
        return SummarizedContent(summary="Mock summary", description="Mock summary")
    if response_model == KnowledgeGraph:
        name = next((t for t in (ALPHA, BRAVO) if text_input and t in text_input), None)
        nodes = (
            [KGNode(id=name, name=name, type="Company", description=f"{name} entity")]
            if name
            else []
        )
        return KnowledgeGraph(nodes=nodes, edges=[])
    return response_model()


async def _graph_has(token: str) -> bool:
    nodes, _ = await (await get_graph_engine()).get_graph_data()
    token = token.lower()
    return any(
        token in str(value).lower() for _id, props in nodes for value in (props or {}).values()
    )


async def _documents() -> list:
    from cognee.modules.data.methods import get_authorized_existing_datasets, get_dataset_data
    from cognee.modules.users.methods import get_default_user

    user = await get_default_user()
    datasets = await get_authorized_existing_datasets(
        user=user, permission_type="read", datasets=[DATASET]
    )
    return await get_dataset_data(datasets[0].id)


@pytest_asyncio.fixture
async def clean_environment(tmp_path, monkeypatch):
    pytest.importorskip("ladybug")

    # Run on the mocked LLM pipeline, never the keyless local-model reroute.
    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
    monkeypatch.setenv("GRAPH_EXTRACTOR", "llm")
    cognee.config.data_root_directory(str(tmp_path / "data"))
    cognee.config.system_root_directory(str(tmp_path / "system"))
    cognee.config.set_relational_db_config({"db_provider": "sqlite"})

    async def _noop_index(*_a, **_k):
        return None

    monkeypatch.setattr(add_data_points_module, "index_data_points", _noop_index)
    monkeypatch.setattr(add_data_points_module, "index_graph_edges", _noop_index)
    monkeypatch.setattr(LLMGateway, "acreate_structured_output", _mock_structured_output)

    async def _mock_embed_text(self, text):
        return [[0.0] * self.get_vector_size() for _ in text]

    monkeypatch.setattr(LiteLLMEmbeddingEngine, "embed_text", _mock_embed_text)

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    yield
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)


async def _sync(wiki: FakeWiki):
    source = mediawiki_source(API_URL, http_client=wiki.client(), revision_history=0)
    await cognee.add(
        source,
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    await cognee.cognify(datasets=[DATASET])
    return source


@pytest.mark.asyncio
async def test_deleting_a_page_upstream_forgets_its_graph_content(clean_environment):
    wiki = FakeWiki()
    wiki.create(1, "Alpha", f"{ALPHA} is a company in the logistics sector.")
    wiki.create(2, "Bravo", f"{BRAVO} is an unrelated company in the finance sector.")
    wiki.tick(60)

    source = await _sync(wiki)
    assert source.cognee_sync_stats["mode"] == "full"
    assert await _graph_has(ALPHA) and await _graph_has(BRAVO)
    # Every page sits under the wiki's node set, so recall can be scoped to it.
    assert await _graph_has("mediawiki:wiki.example.org")

    documents = await _documents()
    assert len(documents) == 2
    assert {d.system_metadata["source"] for d in documents} == {"mediawiki"}
    assert {d.system_metadata["external_id"] for d in documents} == {"1", "2"}
    assert {d.system_metadata["url"] for d in documents} == {
        "https://wiki.example.org/wiki/Alpha",
        "https://wiki.example.org/wiki/Bravo",
    }
    ids_before = {d.id for d in documents}

    # Nothing changed upstream: same documents, no new ones.
    wiki.tick(5)
    source = await _sync(wiki)
    assert source.cognee_sync_stats["mode"] == "incremental"
    assert source.cognee_sync_stats["pages_changed"] == 0
    assert {d.id for d in await _documents()} == ids_before

    wiki.delete(2)
    source = await _sync(wiki)
    assert source.cognee_sync_stats["deleted"] == 1

    assert await _graph_has(ALPHA), "the surviving page's entity must remain"
    assert not await _graph_has(BRAVO), "the deleted page's entity must leave the graph"
    assert {d.system_metadata["external_id"] for d in await _documents()} == {"1"}


EXAMPLE_PATH = pathlib.Path(__file__).parents[1] / "examples" / "example.py"


@pytest.mark.asyncio
async def test_example_runs_without_live_credentials(clean_environment, monkeypatch, capsys):
    """The packaged example runs end to end against a fake wiki and a mocked LLM."""
    wiki = FakeWiki()
    wiki.create(1, "Ada Lovelace", f"{ALPHA} published the notes on the engine.")
    wiki.create(2, "Charles Babbage", "Babbage designed the analytical engine.")
    wiki.create(3, "Analytical engine", "A proposed mechanical computer.")
    wiki.tick(60)

    spec = importlib.util.spec_from_file_location("mediawiki_example", EXAMPLE_PATH)
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)

    def offline_source(*args, **kwargs):
        return mediawiki_source(*args, **kwargs, http_client=wiki.client())

    # recall() searches the vector index, so index for real. Embeddings stay
    # mocked, as a constant non-zero vector so every chunk is a match.
    for name, function in _REAL_INDEXING.items():
        monkeypatch.setattr(add_data_points_module, name, function)

    async def _constant_embed_text(self, text):
        return [[1.0] * self.get_vector_size() for _ in text]

    monkeypatch.setattr(LiteLLMEmbeddingEngine, "embed_text", _constant_embed_text)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setattr(example, "mediawiki_source", offline_source)
    monkeypatch.setattr(example, "API_URL", API_URL)
    await example.main()

    output = capsys.readouterr().out
    assert "full sync: 3 page(s) ingested" in output
    # The synced pages are searchable: recall answers instead of finding nothing.
    assert "\nAnswer: " in output and "(nothing found)" not in output
