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
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
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


def _reset_cached_engines() -> None:
    """Drop cognee's cached engines and dlt's active pipeline, so each test starts
    from its own storage and sync state."""
    from cognee.context_global_variables import graph_db_config, vector_db_config
    from cognee.infrastructure.databases.graph.get_graph_engine import _create_graph_engine
    from cognee.infrastructure.databases.relational.create_relational_engine import (
        create_relational_engine,
    )
    from cognee.infrastructure.databases.vector.create_vector_engine import _create_vector_engine
    from cognee.tasks.ingestion.get_dlt_destination import get_dlt_destination
    from dlt.common.configuration.container import Container
    from dlt.common.pipeline import PipelineContext

    Container()[PipelineContext].deactivate()
    _create_graph_engine.cache_clear()
    _create_vector_engine.cache_clear()
    create_relational_engine.cache_clear()
    get_dlt_destination.cache_clear()
    graph_db_config.set(None)
    vector_db_config.set(None)


@pytest_asyncio.fixture
async def clean_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "databases"))
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt"))
    monkeypatch.setenv("PIPELINES_DIR", str(tmp_path / "dlt" / "pipelines"))
    _reset_cached_engines()
    cognee.config.data_root_directory(str(tmp_path / "data"))
    cognee.config.system_root_directory(str(tmp_path / "system"))
    cognee.config.set_relational_db_config({"db_provider": "sqlite"})

    async def _noop_index(*_a, **_k):
        return None

    monkeypatch.setattr(add_data_points_module, "index_data_points", _noop_index)
    monkeypatch.setattr(add_data_points_module, "index_graph_edges", _noop_index)
    monkeypatch.setattr(LLMGateway, "acreate_structured_output", _mock_structured_output)

    async def _mock_embed_text(self, text):
        vectors = [[0.0] * self.get_vector_size() for _ in text]
        try:
            # newer cognee reads the token count off the result
            from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import (
                EmbeddingResult,
            )
        except ImportError:
            return vectors
        return EmbeddingResult(vectors, tokens=0)

    monkeypatch.setattr(LiteLLMEmbeddingEngine, "embed_text", _mock_embed_text)

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    yield
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    _reset_cached_engines()


async def _documents() -> dict[tuple[str, str], object]:
    """The dataset's Data records, keyed by (table, chat_id:message_id)."""
    user = await get_default_user()
    datasets = await get_authorized_existing_datasets(
        user=user, permission_type="read", datasets=[DATASET]
    )
    return {
        (data.system_metadata["table_name"], data.system_metadata["external_id"]): data.id
        for data in await get_dataset_data(datasets[0].id)
        if isinstance(data.system_metadata, dict)
    }


async def _sync(bot, token="111:test"):
    await cognee.add(
        telegram_source(token=token, client=bot),
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
    before = await _documents()
    kept = ("telegram_111", "-100:1")
    assert set(before) == {kept, ("telegram_111", "-100:2"), ("telegram_111", "-555:1")}

    bot.add(edited_message=msg(GROUP, 2, "Price still open."))
    bot.add(my_chat_member=removed(OTHER, "kicked"))
    await _sync(bot)

    after = await _documents()
    assert set(after) == {kept, ("telegram_111", "-100:2")}, (
        "the messages of a chat the bot was removed from must lose their Data records"
    )
    assert after[kept] == before[kept], "an untouched message must keep its id"

    assert await _graph_has(ALPHA), "an untouched message must stay"
    assert not await _graph_has(BRAVO), "text edited out of a message must be forgotten"
    assert not await _graph_has(CHARLIE), "messages of a chat the bot left must be forgotten"


@pytest.mark.asyncio
async def test_two_bots_in_one_dataset_stay_apart(clean_environment):
    # Both bots sit in the same group, so they see the same chat_id:message_id keys.
    first, second = FakeBot(), FakeBot()
    first.add(message=msg(GROUP, 1, f"{ALPHA} signed the contract."))
    second.add(message=msg(GROUP, 1, f"{ALPHA} signed the contract."))

    await _sync(first, "111:first")
    await _sync(second, "222:second")
    both = {("telegram_111", "-100:1"), ("telegram_222", "-100:1")}
    assert set(await _documents()) == both

    second.add(my_chat_member=removed(GROUP, "kicked"))
    await _sync(second, "222:second")
    await _sync(first, "111:first")

    assert set(await _documents()) == {("telegram_111", "-100:1")}, (
        "removing the second bot must not forget what the first bot still sees"
    )
    assert await _graph_has(ALPHA)
