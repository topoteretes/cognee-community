"""End-to-end: Ahrefs changes must reach cognee's graph, not just dlt rows.

Runs the real cognee.remember (dlt merge -> relational manifest -> cognify)
against the fake Ahrefs API, with a mocked LLM and mocked embeddings, so no
network or API key is needed. Each backlink comes from a distinctive domain,
which the graph holds as part of the row.
"""

import importlib
from datetime import date

import cognee
import pytest
import pytest_asyncio
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.infrastructure.databases.vector.embeddings.LiteLLMEmbeddingEngine import (
    LiteLLMEmbeddingEngine,
)
from cognee.infrastructure.llm import LLMGateway
from fake_ahrefs import FakeAhrefs

import cognee_community_connector_ahrefs.ahrefs as module
from cognee_community_connector_ahrefs import ahrefs_source

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")

DATASET = "ahrefs_forget_test"
TARGET = "example.com"
ALPHA = "alphacorp"
BRAVO = "bravocorp"


def _with_required(model, **values):
    """Build a cognee data model, filling any other required text field."""
    for field_name, field in model.model_fields.items():
        if field.is_required() and field_name not in values:
            values[field_name] = "mock"
    return model(**values)


async def _mock_structured_output(
    text_input=None, system_prompt=None, response_model=str, **_kwargs
):
    """The relational path needs no LLM; answer anything that still asks."""
    from cognee.shared.data_models import KnowledgeGraph, SummarizedContent

    if response_model is str:
        return "Mocked answer."
    if response_model == SummarizedContent:
        return SummarizedContent(summary="Mock summary", description="Mock summary")
    if response_model == KnowledgeGraph:
        return _with_required(KnowledgeGraph, nodes=[], edges=[])
    return response_model()


async def _graph_has(token: str) -> bool:
    nodes, _ = await (await get_graph_engine()).get_graph_data()
    token = token.lower()
    return any(
        token in str(value).lower() for _nid, props in nodes for value in (props or {}).values()
    )


def _link(domain, **extra):
    return {
        "url_from": f"https://{domain}.test/review",
        "url_to": f"https://{TARGET}/",
        "anchor": "great tool",
        "domain_rating_source": 60.0,
        "is_dofollow": True,
        "first_seen_link": "2026-03-04T05:06:07Z",
        "last_visited": "2026-10-01T00:00:00Z",
        "is_lost": False,
        **extra,
    }


@pytest_asyncio.fixture
async def clean_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
    monkeypatch.setattr(module, "_today", lambda: date(2026, 10, 15))
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


async def _sync(api):
    sources = ahrefs_source(
        [TARGET],
        reports=["domain_rating", "backlinks"],
        api_key="test-key",
        session=api.session(),
    )
    await cognee.remember(
        sources,
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
        # A report keeps one data id across syncs, so it must not be skipped.
        incremental_loading=False,
        data_cache=False,
    )
    return sources


@pytest.mark.asyncio
async def test_a_lost_backlink_is_forgotten_and_the_rest_stays(clean_environment):
    api = FakeAhrefs()
    api.domain_ratings[TARGET] = [{"date": "2026-10-01", "domain_rating": 72.5}]
    api.backlinks[TARGET] = [_link(ALPHA), _link(BRAVO)]

    await _sync(api)
    assert await _graph_has(ALPHA), "a live backlink should be in the graph"
    assert await _graph_has(BRAVO), "a live backlink should be in the graph"
    assert await _graph_has("72.5"), "the Domain Rating point should be in the graph"

    api.backlinks[TARGET] = [_link(ALPHA), _link(BRAVO, is_lost=True, last_seen="2026-10-10")]
    sources = await _sync(api)

    assert sources[1].cognee_sync_stats["deleted"] == 1
    assert await _graph_has(ALPHA), "an untouched backlink must stay"
    assert not await _graph_has(BRAVO), "a lost backlink must be forgotten from the graph"
    assert await _graph_has("72.5"), "another report must not be touched"
