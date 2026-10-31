"""End-to-end: a resolved issue must leave cognee's graph, not just dlt rows.

Runs the real cognee pipeline (dlt merge -> document ingestion -> cognify)
against the fake SonarQube session, with a mocked LLM and mocked embeddings,
so no network or API key is needed. Each issue message carries one
distinctive token, which the mocked LLM turns into exactly one graph entity.
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
from fake_sonarqube import FakeSonarQubeSession, issue

from cognee_community_connector_sonarqube import sonarqube_source

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")

DATASET = "sonarqube_forget_test"
ALPHA = "Alphatoken"
BRAVO = "Bravotoken"
TOKENS = (ALPHA, BRAVO)


def _with_required(model, **values):
    for field_name, field in model.model_fields.items():
        if field.is_required() and field_name not in values:
            values[field_name] = "mock"
    return model(**values)


async def _mock_structured_output(
    text_input=None, system_prompt=None, response_model=str, **_kwargs
):
    from cognee.shared.data_models import KnowledgeGraph, SummarizedContent
    from cognee.shared.data_models import Node as KGNode

    if response_model is str:
        return "Mocked answer."
    if response_model == SummarizedContent:
        return SummarizedContent(summary="Mock summary", description="Mock summary")
    if response_model == KnowledgeGraph:
        name = next((t for t in TOKENS if text_input and t in text_input), None)
        nodes = (
            [_with_required(KGNode, id=name, name=name, type="Issue", description=f"{name} entity")]
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


async def _sync(session):
    await cognee.add(
        sonarqube_source(
            token="tok",
            base_url="https://sonarqube.example.com",
            project_keys=["proj"],
            session=session,
        ),
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    await cognee.cognify(datasets=[DATASET])


@pytest.mark.asyncio
async def test_resolved_issue_is_forgotten_from_the_graph(clean_environment):
    session = FakeSonarQubeSession(
        projects={
            "proj": {
                "name": "Proj",
                "issues": [
                    issue("A", message=f"{ALPHA} needs a fix."),
                    issue("B", message=f"{BRAVO} needs a fix."),
                ],
            }
        }
    )

    await _sync(session)
    for token in TOKENS:
        assert await _graph_has(token), f"{token} should be in the graph after the first sync"

    session.projects["proj"]["issues"] = [
        issue("A", message=f"{ALPHA} needs a fix."),
        issue("B", message=f"{BRAVO} needs a fix.", resolution="FIXED"),
    ]
    await _sync(session)

    assert await _graph_has(ALPHA), "an untouched issue must stay in the graph"
    assert not await _graph_has(BRAVO), "a resolved issue must be forgotten from the graph"
