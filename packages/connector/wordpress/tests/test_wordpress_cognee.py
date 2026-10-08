"""End-to-end through cognee: an item deleted upstream leaves the graph.

Syncs a fake WordPress site with ``cognee.add`` + ``cognify`` (LLM and
embeddings mocked, no credentials), deletes one post on the site, syncs again,
and checks that the deleted post's extracted entity is gone from the graph
while the surviving post's entity remains. It also checks the documents land
as ``wordpress`` documents under the site's node set, that an unchanged
re-sync does not re-ingest anything, and that the packaged example runs.
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
from fake_wordpress import SITE_URL, FakeWordPress

from cognee_community_connector_wordpress import wordpress_source

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")
_REAL_INDEXING = {
    name: getattr(add_data_points_module, name)
    for name in ("index_data_points", "index_graph_edges")
}

DATASET = "wordpress_forget_test"
# Distinctive tokens so each post maps to exactly one graph entity.
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


async def _sync(wp: FakeWordPress):
    source = wordpress_source(SITE_URL, http_client=wp.client())
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
async def test_deleting_a_post_upstream_forgets_its_graph_content(clean_environment):
    wp = FakeWordPress()
    alpha = wp.add("post", "Alpha", f"<p>{ALPHA} is a company in the logistics sector.</p>")
    bravo = wp.add("post", "Bravo", f"<p>{BRAVO} is an unrelated company in finance.</p>")
    wp.tick(3600)

    source = await _sync(wp)
    assert source.cognee_sync_stats["mode"] == "full"
    assert await _graph_has(ALPHA) and await _graph_has(BRAVO)
    # Every item sits under the site's node set, so recall can be scoped to it.
    assert await _graph_has("wordpress:blog.example.com")

    documents = await _documents()
    assert len(documents) == 2
    assert {d.system_metadata["source"] for d in documents} == {"wordpress"}
    assert {d.system_metadata["external_id"] for d in documents} == {str(alpha.id), str(bravo.id)}
    assert {d.system_metadata["url"] for d in documents} == {
        f"{SITE_URL}/?p={alpha.id}",
        f"{SITE_URL}/?p={bravo.id}",
    }
    ids_before = {d.id for d in documents}

    # Nothing changed upstream: same documents, no new ones.
    wp.tick(600)
    source = await _sync(wp)
    assert source.cognee_sync_stats["mode"] == "incremental"
    assert source.cognee_sync_stats["items_changed"] == 0
    assert {d.id for d in await _documents()} == ids_before

    wp.delete(bravo.id)
    source = await _sync(wp)
    assert source.cognee_sync_stats["deleted"] == 1

    assert await _graph_has(ALPHA), "the surviving post's entity must remain"
    assert not await _graph_has(BRAVO), "the deleted post's entity must leave the graph"
    assert {d.system_metadata["external_id"] for d in await _documents()} == {str(alpha.id)}


EXAMPLE_PATH = pathlib.Path(__file__).parents[1] / "examples" / "example.py"


@pytest.mark.asyncio
async def test_example_runs_without_live_credentials(clean_environment, monkeypatch, capsys):
    """The packaged example runs end to end against a fake site and a mocked LLM."""
    wp = FakeWordPress()
    wp.add("post", "Launch notes", f"<p>{ALPHA} launched its first rocket in 2019.</p>")
    wp.add("post", "Hiring", "<p>We are hiring rocket engineers.</p>")
    wp.add("page", "About", "<p>A blog about rockets.</p>")
    wp.tick(3600)

    spec = importlib.util.spec_from_file_location("wordpress_example", EXAMPLE_PATH)
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)

    def offline_source(*args, **kwargs):
        return wordpress_source(*args, **kwargs, http_client=wp.client())

    # recall() searches the vector index, so index for real. Embeddings stay
    # mocked, as a constant non-zero vector so every chunk is a match.
    for name, function in _REAL_INDEXING.items():
        monkeypatch.setattr(add_data_points_module, name, function)

    async def _constant_embed_text(self, text):
        return [[1.0] * self.get_vector_size() for _ in text]

    monkeypatch.setattr(LiteLLMEmbeddingEngine, "embed_text", _constant_embed_text)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setattr(example, "SCOPE", {})  # the fake site has no demo category
    monkeypatch.setattr(example, "wordpress_source", offline_source)
    monkeypatch.setattr(example, "SITE_URL", SITE_URL)
    await example.main()

    output = capsys.readouterr().out
    assert "full sync: 3 item(s) ingested" in output
    # The synced items are searchable: recall answers instead of finding nothing.
    assert "\nAnswer: " in output and "(nothing found)" not in output
