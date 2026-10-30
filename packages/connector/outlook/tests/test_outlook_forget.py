"""End-to-end: Outlook changes must reach cognee's graph, not just dlt rows.

Runs the real cognee pipeline (dlt merge -> document ingestion -> cognify)
against the fake Graph, with a mocked LLM and mocked embeddings, so no network
or API key is needed.  Each message and event carries one distinctive company
name, which the mocked LLM turns into exactly one graph entity.
"""

import importlib
from datetime import datetime, timedelta, timezone

import cognee
import pytest
import pytest_asyncio
import responses
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.infrastructure.databases.vector.embeddings.LiteLLMEmbeddingEngine import (
    LiteLLMEmbeddingEngine,
)
from cognee.infrastructure.llm import LLMGateway
from cognee.modules.data.methods import get_dataset_data, get_datasets_by_name
from cognee.modules.users.methods import get_default_user
from fake_graph import FakeGraph

from cognee_community_connector_outlook import outlook_source

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")

try:
    # Newer cognee returns vectors plus a token count; 1.6.3 a plain list.
    from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import (
        EmbeddingResult,
    )
except ImportError:
    EmbeddingResult = None

DATASET = "outlook_forget_test"
ALPHA = "Alphacorp"
BRAVO = "Bravocorp"
CHARLIE = "Charliecorp"
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
            [
                _with_required(
                    KGNode, id=name, name=name, type="Company", description=f"{name} entity"
                )
            ]
            if name
            else []
        )
        return _with_required(KnowledgeGraph, nodes=nodes, edges=[])
    return response_model()


def _with_required(model, **values):
    """Build a cognee data model, filling any other required text field.

    cognee swaps in Node and KnowledgeGraph models with extra required fields
    (label, summary, description) when the configured LLM provider is Gemini,
    so the mock must work with both variants.
    """
    for field_name, field in model.model_fields.items():
        if field.is_required() and field_name not in values:
            values[field_name] = "mock"
    return model(**values)


async def _graph_has(token: str) -> bool:
    nodes, _ = await (await get_graph_engine()).get_graph_data()
    token = token.lower()
    return any(
        token in str(value).lower() for _nid, props in nodes for value in (props or {}).values()
    )


@pytest.fixture(scope="session")
def cognee_root(tmp_path_factory):
    # One root for the whole session: cognee caches its database engines per
    # process, so a new root per test would leave later tests reading the
    # first test's stores.  prune() below empties it between tests instead.
    return tmp_path_factory.mktemp("cognee")


@pytest_asyncio.fixture
async def clean_environment(cognee_root, tmp_path, monkeypatch):
    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
    # dlt keeps pipeline state under ~/.dlt, which cognee's prune never clears,
    # so without this a second test run would start from the first run's tokens.
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt"))
    cognee.config.data_root_directory(str(cognee_root / "data"))
    cognee.config.system_root_directory(str(cognee_root / "system"))
    cognee.config.set_relational_db_config({"db_provider": "sqlite"})

    async def _noop_index(*_a, **_k):
        return None

    monkeypatch.setattr(add_data_points_module, "index_data_points", _noop_index)
    monkeypatch.setattr(add_data_points_module, "index_graph_edges", _noop_index)
    monkeypatch.setattr(LLMGateway, "acreate_structured_output", _mock_structured_output)

    async def _mock_embed_text(self, text):
        vectors = [[0.0] * self.get_vector_size() for _ in text]
        return EmbeddingResult(vectors, tokens=0) if EmbeddingResult else vectors

    monkeypatch.setattr(LiteLLMEmbeddingEngine, "embed_text", _mock_embed_text)

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    yield
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)


async def _stored_documents() -> int:
    """How many data items the dataset holds.

    A tombstone that only empties a row (hard_delete off) leaves an empty
    document behind, which the graph checks alone would not notice.
    """
    user = await get_default_user()
    datasets = await get_datasets_by_name([DATASET], user.id)
    return len(await get_dataset_data(datasets[0].id))


async def _sync(graph, resource_name="outlook_items"):
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        graph.install(mock)
        await cognee.add(
            outlook_source(
                mail_folders=["inbox", "archive"],
                access_token="test-token",
                base_url=graph.base,
                resource_name=resource_name,
            ),
            dataset_name=DATASET,
            primary_key="id",
            write_disposition="merge",
            max_rows_per_table=0,
        )
    await cognee.cognify(datasets=[DATASET])


@pytest.mark.asyncio
async def test_deleted_mail_and_event_are_forgotten_moved_mail_is_kept(clean_environment):
    soon = datetime.now(timezone.utc) + timedelta(days=2)
    graph = FakeGraph()
    alpha = graph.put_message("inbox", "Logistics", f"{ALPHA} is a company in logistics.")
    bravo = graph.put_message("inbox", "Finance", f"{BRAVO} is a company in finance.")
    meeting = graph.put_event(
        "Rocket review", soon, body={"contentType": "text", "content": f"{CHARLIE} rockets."}
    )

    await _sync(graph)
    for token in TOKENS:
        assert await _graph_has(token), f"{token} should be in the graph after the first sync"
    assert await _stored_documents() == 3

    graph.move_message(alpha, "inbox", "archive")  # a move between synced folders
    graph.delete_message(bravo, "inbox")  # Outlook moves it to Deleted Items
    graph.delete_event(meeting)
    await _sync(graph)

    assert await _graph_has(ALPHA), "a message moved between synced folders must stay"
    assert not await _graph_has(BRAVO), "a deleted message must be forgotten from the graph"
    assert not await _graph_has(CHARLIE), "a deleted meeting must be forgotten from the graph"
    assert await _stored_documents() == 1, "deleted items must be removed, not left empty"


@pytest.mark.asyncio
async def test_two_mailboxes_in_one_dataset_stay_apart(clean_environment):
    work = FakeGraph()
    personal = FakeGraph(base="https://graph-personal.test/v1.0")
    alpha = work.put_message("inbox", "Logistics", f"{ALPHA} is a company in logistics.")
    personal.put_message("inbox", "Finance", f"{BRAVO} is a company in finance.")

    # Each sync of one mailbox must leave the other mailbox's mail alone.
    await _sync(work, "work_outlook")
    await _sync(personal, "personal_outlook")
    await _sync(work, "work_outlook")
    assert await _graph_has(ALPHA) and await _graph_has(BRAVO)
    assert await _stored_documents() == 2

    work.delete_message(alpha, "inbox")
    await _sync(work, "work_outlook")

    assert not await _graph_has(ALPHA)
    assert await _graph_has(BRAVO), "a delete in one mailbox must not touch the other"
    assert await _stored_documents() == 1
