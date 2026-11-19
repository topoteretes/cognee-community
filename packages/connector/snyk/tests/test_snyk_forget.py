"""End-to-end: Snyk findings that are fixed must leave cognee's graph, not just dlt rows.

Runs the real cognee pipeline (dlt merge -> document ingestion -> cognify) against the fake
Snyk, with a mocked LLM and mocked embeddings, so no network or API key is needed. Each
vulnerability carries one distinctive company name in its description, which the mocked LLM
turns into exactly one graph entity.
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
from fake_snyk import FakeSnykSession, make_issue, make_project

from cognee_community_connector_snyk import snyk_source

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")

DATASET = "snyk_forget_test"
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


async def _sync(session):
    source = snyk_source("token-for-tests", org_ids=["o1"], region="us-01", session=session)
    await cognee.add(
        source,
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    await cognee.cognify(datasets=[DATASET])
    return source.cognee_sync_stats


def _vulnerability(n: int, company: str, project: str = "p1") -> dict:
    return make_issue(
        project=project,
        snyk_id=f"SNYK-JS-V{n}",
        cves=(f"CVE-2020-{1000 + n}",),
        title=f"Vulnerability {n}",
        package=f"package{n}",
        description=f"{company} ships a service that is affected.",
        issue_id=f"issue-{n}-{project}",
    )


@pytest.mark.asyncio
async def test_a_fixed_vulnerability_leaves_the_graph_and_the_others_stay(clean_environment):
    session = FakeSnykSession()
    session.add_org(
        "o1",
        "Acme",
        projects=[make_project("p1", "acme/shop"), make_project("p2", "acme/admin")],
        issues=[
            _vulnerability(1, ALPHA),
            _vulnerability(2, BRAVO),
            _vulnerability(2, BRAVO, "p2"),
            _vulnerability(3, CHARLIE),
        ],
    )

    stats = await _sync(session)
    assert stats["emitted"] == 3
    for token in TOKENS:
        assert await _graph_has(token), f"{token} should be in the graph after the first sync"

    # A run that finds nothing new emits no rows. That must not read as "everything vanished".
    stats = await _sync(session)
    assert stats["emitted"] == 0
    assert stats["unchanged"] == 3
    for token in TOKENS:
        assert await _graph_has(token), f"{token} must survive a run that emits no rows"

    # Vulnerability 2 is fixed in both projects: Snyk now reports it as resolved.
    for issue in session.issues["o1"]:
        if issue["id"].startswith("issue-2-"):
            issue["attributes"]["status"] = "resolved"
    stats = await _sync(session)
    assert stats["deleted"] == 1

    assert await _graph_has(ALPHA), "an untouched vulnerability must stay"
    assert await _graph_has(CHARLIE), "an untouched vulnerability must stay"
    assert not await _graph_has(BRAVO), "a fixed vulnerability must be forgotten from the graph"
