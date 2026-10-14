"""Forget-on-delete must reach cognee's graph, not just the dlt rows.

Runs the real cognee pipeline (dlt merge, document ingestion, cognify) against
FakeZendesk, with the LLM and embeddings mocked, so no credentials or API key are
needed. Follows the google-drive connector's forget test: each fact carries one
distinctive company name, which the mocked LLM turns into exactly one entity.
"""

import importlib

import cognee
import pytest
import pytest_asyncio
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.infrastructure.databases.vector.embeddings.LiteLLMEmbeddingEngine import (
    LiteLLMEmbeddingEngine,
)
from cognee.infrastructure.llm import LLMGateway
from fake_zendesk import SUB, FakeZendesk, article, comment, ticket

from cognee_community_connector_zendesk import zendesk_source

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")

DATASET = "zendesk_forget_test"
ALPHA = "Alphacorp"  # in a ticket that stays
BRAVO = "Bravocorp"  # in a ticket that is deleted
CHARLIE = "Charliecorp"  # in a Help Center article that is archived
TOKENS = (ALPHA, BRAVO, CHARLIE)


async def _mock_structured_output(
    text_input=None, system_prompt=None, response_model=str, **_kwargs
):
    """Extract one entity named after whichever token appears in the chunk text."""
    from cognee.shared.data_models import KnowledgeGraph, SummarizedContent
    from cognee.shared.data_models import Node as KGNode

    if response_model is str:
        return "Mocked answer."
    if response_model == SummarizedContent:
        return SummarizedContent(summary="Mock summary", description="Mock summary")
    if response_model == KnowledgeGraph:
        name = next((t for t in TOKENS if text_input and t in text_input), None)
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
        token in str(value).lower() for _nid, props in nodes for value in (props or {}).values()
    )


@pytest_asyncio.fixture
async def clean_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt"))
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


async def _sync(fake):
    await cognee.add(
        zendesk_source(SUB, client=fake),
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    await cognee.cognify(datasets=[DATASET])


@pytest.mark.asyncio
async def test_deleted_ticket_and_archived_article_leave_the_graph(clean_environment):
    fake = FakeZendesk()
    fake.touch(ticket(1, "Invoice question"), [comment(1, f"{ALPHA} needs a new invoice.")])
    fake.touch(ticket(2, "Discount"), [comment(2, f"{BRAVO} asked for 15% off.")])
    fake.articles = [article(3, body=f"<p>{CHARLIE} customers reset passwords here.</p>")]

    await _sync(fake)
    for token in TOKENS:
        assert await _graph_has(token), f"{token} should be in the graph after the first sync"

    fake.touch(ticket(2, "Discount", status="deleted"))
    fake.articles = []  # archived articles disappear from the listing
    await _sync(fake)

    assert await _graph_has(ALPHA), "a ticket that was not deleted must stay"
    assert not await _graph_has(BRAVO), "a deleted ticket must be forgotten from the graph"
    assert not await _graph_has(CHARLIE), "an archived article must be forgotten from the graph"
