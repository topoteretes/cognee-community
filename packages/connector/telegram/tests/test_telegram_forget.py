"""Forget-on-delete must reach cognee's graph, not just the dlt rows.

Runs the real cognee pipeline (dlt merge, document ingestion, cognify) against
FakeBot, with the LLM and embeddings mocked, so no bot token or API key is
needed. Follows the google-drive connector's forget test: each fact carries one
distinctive company name, which the mocked LLM turns into exactly one entity.

Telegram sends no event when a single message is deleted, so the two ways a fact
can leave are an edit (the message is replaced) and the bot being removed from
the chat (every message stored for that chat is forgotten).
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
from fake_telegram import GROUP, OTHER, FakeBot, msg, removed

from cognee_community_connector_telegram import telegram_source

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")

DATASET = "telegram_forget_test"
ALPHA = "Alphacorp"  # in a message that stays
BRAVO = "Bravocorp"  # edited out of a message
CHARLIE = "Charliecorp"  # in a chat the bot is removed from
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


async def _sync(bot):
    await cognee.add(
        telegram_source(client=bot),
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    await cognee.cognify(datasets=[DATASET])


@pytest.mark.asyncio
async def test_edit_and_removal_from_chat_leave_the_graph(clean_environment):
    bot = FakeBot()
    bot.add(message=msg(GROUP, 1, f"{ALPHA} signed the contract."))
    bot.add(message=msg(GROUP, 2, f"Price agreed with {BRAVO}."))
    bot.add(message=msg(OTHER, 1, f"{CHARLIE} is the new supplier."))

    await _sync(bot)
    for token in TOKENS:
        assert await _graph_has(token), f"{token} should be in the graph after the first sync"

    bot.add(edited_message=msg(GROUP, 2, "Price still open."))
    bot.add(my_chat_member=removed(OTHER, "kicked"))
    await _sync(bot)

    assert await _graph_has(ALPHA), "an untouched message must stay"
    assert not await _graph_has(BRAVO), "text edited out of a message must be forgotten"
    assert not await _graph_has(CHARLIE), "messages of a chat the bot left must be forgotten"
