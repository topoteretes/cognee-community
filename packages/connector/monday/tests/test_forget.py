"""End-to-end: monday.com changes must reach cognee's graph, not just dlt rows.

Runs the real cognee pipeline (dlt merge, document ingestion, cognify) against
the fake monday.com, with a mocked LLM and mocked embeddings, so no network or
API key is needed. Each item carries one distinctive company name, which the
mocked LLM turns into exactly one graph entity.
"""

import importlib

import cognee
import pytest
import pytest_asyncio
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.infrastructure.databases.vector.embeddings.LiteLLMEmbeddingEngine import (
    LiteLLMEmbeddingEngine,
)

try:
    from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import (
        EmbeddingResult,
    )
except ImportError:  # cognee before 1.6.5 returns plain lists
    EmbeddingResult = None
from cognee.infrastructure.llm import LLMGateway
from fake_monday import FakeMonday

from cognee_community_connector_monday import monday_source

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")

DATASET = "monday_forget_test"
BOARD, SUBITEMS = "100", "101"
ALPHA, BRAVO, CHARLIE, DELTA, ECHO = (
    "Alphacorp",
    "Bravocorp",
    "Charliecorp",
    "Deltacorp",
    "Echocorp",
)
TOKENS = (ALPHA, BRAVO, CHARLIE, DELTA, ECHO)


def _with_required(model, **values):
    """Build a cognee data model, filling any other required text field.

    cognee swaps in Node and KnowledgeGraph models with extra required fields
    when the configured LLM provider is Gemini, so the mock works with both.
    """
    for field_name, field in model.model_fields.items():
        if field.is_required() and field_name not in values:
            values[field_name] = "mock"
    return model(**values)


async def _mock_structured_output(
    text_input=None, system_prompt=None, response_model=str, **_kwargs
):
    """Extract one entity per company name that appears in the chunk text."""
    from cognee.shared.data_models import KnowledgeGraph, SummarizedContent
    from cognee.shared.data_models import Node as KGNode

    if response_model is str:
        return "Mocked answer."
    if response_model == SummarizedContent:
        return SummarizedContent(summary="Mock summary", description="Mock summary")
    if response_model == KnowledgeGraph:
        names = [t for t in TOKENS if text_input and t in text_input]
        nodes = [
            _with_required(KGNode, id=name, name=name, type="Company", description=name)
            for name in names
        ]
        return _with_required(KnowledgeGraph, nodes=nodes, edges=[])
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
    # dlt keeps each pipeline's state under ~/.dlt by default, which would carry
    # the connector's saved stamps from one test (and one test run) to the next.
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
        vectors = [[0.0] * self.get_vector_size() for _ in text]
        # From cognee 1.6.5 an engine returns its vectors with the token count.
        return EmbeddingResult(vectors, tokens=0) if EmbeddingResult else vectors

    monkeypatch.setattr(LiteLLMEmbeddingEngine, "embed_text", _mock_embed_text)

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    yield
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)


def _board():
    monday = FakeMonday()
    monday.add_board(BOARD, "Partners", subitems_board=SUBITEMS)
    monday.add_board(SUBITEMS, "Subitems of Partners")
    monday.add_item("1", BOARD, f"Contract with {ALPHA}", status="Working on it")
    monday.add_item("2", BOARD, f"Invoice {BRAVO}", status="Done")
    monday.add_item("3", SUBITEMS, f"Demo for {CHARLIE}", parent="1")
    monday.add_item("4", BOARD, f"Lunch with {ECHO}")
    return monday


async def _sync(source):
    await cognee.add(
        source,
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    await cognee.cognify(datasets=[DATASET])


async def _in_graph(*tokens):
    return {token: await _graph_has(token) for token in tokens}


# One test with several syncs, like the Google Calendar connector's: cognee
# caches its database engines per process, so a second test in the same
# process would still talk to the first test's (already removed) databases.
@pytest.mark.asyncio
async def test_monday_changes_reach_the_graph(clean_environment):
    monday = _board()
    # One source object across syncs, as a host would keep it.
    source = monday_source(board_ids=[BOARD], client=monday)

    # Run 1: everything comes in, sub-items included.
    await _sync(source)
    assert await _in_graph(ALPHA, BRAVO, CHARLIE, ECHO) == dict.fromkeys(
        (ALPHA, BRAVO, CHARLIE, ECHO), True
    )

    # Run 2: an item deleted, a sub-item archived, and a comment posted. The
    # comment does not move its item's updated_at and must still be picked up.
    monday.delete("2")
    monday.archive("3")
    monday.post_update("1", f"{DELTA} joined the contract")
    await _sync(source)
    assert await _in_graph(ALPHA, BRAVO, CHARLIE, DELTA, ECHO) == {
        ALPHA: True,  # untouched content stays
        BRAVO: False,  # deleted item is forgotten
        CHARLIE: False,  # archived sub-item is forgotten
        DELTA: True,  # the new comment reached the graph
        ECHO: True,
    }
    assert source.cognee_sync_stats == {"scanned": 2, "changed": 1, "deleted": 2}

    # Run 3: a sync where a deletion is the only change.
    monday.delete("4")
    await _sync(source)
    assert await _in_graph(ALPHA, DELTA, ECHO) == {ALPHA: True, DELTA: True, ECHO: False}
    assert source.cognee_sync_stats == {"scanned": 1, "deleted": 1}
