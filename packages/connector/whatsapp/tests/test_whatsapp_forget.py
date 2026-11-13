"""End-to-end: WhatsApp export changes must reach cognee's graph, not just dlt rows.

Runs the real cognee pipeline (dlt merge -> document ingestion -> cognify) on
synthetic exports, with a mocked LLM and mocked embeddings, so no network or
API key is needed. Each day carries one distinctive company name, which the
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
from cognee.infrastructure.llm import LLMGateway

from cognee_community_connector_whatsapp import whatsapp_source

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")

DATASET = "whatsapp_forget_test"
ALPHA = "Alphacorp"
BRAVO = "Bravocorp"
CHARLIE = "Charliecorp"
TOKENS = (ALPHA, BRAVO, CHARLIE)


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


async def _graph_has(token: str) -> bool:
    nodes, _ = await (await get_graph_engine()).get_graph_data()
    token = token.lower()
    return any(
        token in str(value).lower() for _nid, props in nodes for value in (props or {}).values()
    )


@pytest_asyncio.fixture
async def clean_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
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


async def _sync(path):
    await cognee.add(
        whatsapp_source(path),
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    await cognee.cognify(datasets=[DATASET])


@pytest.mark.asyncio
async def test_window_aware_forget_reaches_the_graph(clean_environment, tmp_path):
    export = tmp_path / "WhatsApp Chat with Suppliers.txt"
    export.write_text(
        f"13/10/26, 09:00 - Asha: {ALPHA} quoted 420 per kg\n"
        f"14/10/26, 09:00 - Ravi: {BRAVO} can deliver on Friday\n"
        f"15/10/26, 09:00 - Asha: {CHARLIE} sent the invoice\n",
        encoding="utf-8",
    )
    await _sync(export)
    for token in TOKENS:
        assert await _graph_has(token), f"{token} should be in the graph after the first sync"
    # Each chat is its own node set, namespaced under the source tag.
    assert await _graph_has("whatsapp:Suppliers"), "the chat's node set should be in the graph"

    # A newer export no longer reaches back to the 13th (as when WhatsApp's cap
    # cuts it), and the only message on the 15th was deleted for everyone.
    export.write_text(
        f"14/10/26, 09:00 - Ravi: {BRAVO} can deliver on Friday\n"
        "15/10/26, 09:00 - Asha: This message was deleted\n"
        "16/10/26, 09:00 - Ravi: See you on Friday\n",
        encoding="utf-8",
    )
    await _sync(export)

    assert await _graph_has(ALPHA), "a day before the new export's window must be kept"
    assert await _graph_has(BRAVO), "an unchanged day must stay"
    assert not await _graph_has(CHARLIE), "a day emptied inside the window must be forgotten"
