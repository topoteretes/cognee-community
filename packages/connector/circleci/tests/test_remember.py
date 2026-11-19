"""cognee.remember end to end: pipelines reach memory, update in place, and are forgotten.

Same technique as google-drive's test_google_drive_forget.py: real dlt and real cognee
storage and graph, with the LLM and embeddings stubbed so no keys are needed. The
stubbed extraction returns one entity per pipeline number in a chunk ("pipeline #3"
becomes ``cipipeline3``), which shows what is in the graph after each sync.
"""

import asyncio
import importlib
import json
import re

import cognee
import pytest
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.infrastructure.databases.vector.embeddings.LiteLLMEmbeddingEngine import (
    LiteLLMEmbeddingEngine,
)
from cognee.infrastructure.llm import LLMGateway

from cognee_community_connector_circleci import circleci_source

SLUG = "gh/rokadepiyush49-rgb/cognee-circleci-fixture"
OTHER = "gh/acme/other"
SLOW_RERUN = "4c2bfb04-d2c8-4449-a78a-ea76aae951c9"  # pipeline #6

# A second project with one finished pipeline, so a dataset can outlive the
# fixture project (cognee skips orphan cleanup when a sync leaves it empty).
OTHER_PIPELINE = {
    "id": "other-77",
    "number": 77,
    "project_slug": OTHER,
    "state": "created",
    "created_at": "2026-10-07T20:00:00.000Z",
    "errors": [],
    "trigger": {"type": "webhook", "actor": {"login": "alice"}},
    "vcs": {"branch": "main", "revision": "abcdef1234567", "commit": {"subject": "Other work"}},
}

_PIPELINE_NUMBER = re.compile(r"pipeline #(\d+)", re.IGNORECASE)
_ENTITY = re.compile(r"cipipeline\d+", re.IGNORECASE)


async def _mock_structured_output(
    text_input=None, system_prompt=None, response_model=str, **_kwargs
):
    """Extract one entity per pipeline number that appears in the chunk."""
    from cognee.shared.data_models import KnowledgeGraph, SummarizedContent
    from cognee.shared.data_models import Node as KGNode

    if response_model is str:
        return "Mocked answer."
    if response_model == SummarizedContent:
        return SummarizedContent(summary="Mock summary", description="Mock summary")
    if response_model == KnowledgeGraph:
        numbers = sorted(set(_PIPELINE_NUMBER.findall(text_input or "")))
        nodes = [
            KGNode(id=f"cipipeline{n}", name=f"CIPipeline{n}", type="Pipeline", description="")
            for n in numbers
        ]
        return KnowledgeGraph(nodes=nodes, edges=[])
    return response_model()


async def _mock_embed_text(self, text):
    return [[0.0] * self.get_vector_size() for _ in text]


async def _noop(*_args, **_kwargs):
    return None


@pytest.fixture
def cognee_env(tmp_path, monkeypatch):
    pytest.importorskip("ladybug")
    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
    # The connector's sync state lives in cognee's dlt pipeline; keep it per test.
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt"))
    cognee.config.data_root_directory(str(tmp_path / "data"))
    cognee.config.system_root_directory(str(tmp_path / "system"))
    cognee.config.set_relational_db_config({"db_provider": "sqlite"})

    add_data_points = importlib.import_module("cognee.tasks.storage.add_data_points")
    monkeypatch.setattr(add_data_points, "index_data_points", _noop)
    monkeypatch.setattr(add_data_points, "index_graph_edges", _noop)
    monkeypatch.setattr(LLMGateway, "acreate_structured_output", _mock_structured_output)
    monkeypatch.setattr(LiteLLMEmbeddingEngine, "embed_text", _mock_embed_text)


async def _prune():
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)


async def _remember(session, dataset, slugs=(SLUG,), resource_name="circleci_pipelines"):
    await cognee.remember(
        circleci_source(project_slugs=list(slugs), service=session, resource_name=resource_name),
        dataset_name=dataset,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
        self_improvement=False,
    )


async def _memory(dataset):
    """({pipeline id: document title}, {pipeline entities in the graph})."""
    datasets = [d for d in await cognee.datasets.list_datasets() if d.name == dataset]
    documents = await cognee.datasets.list_data(datasets[0].id) if datasets else []
    titles = {
        meta["external_id"]: meta["title"]
        for meta in map(_document_metadata, documents)
        if meta.get("source") == "circleci"
    }
    nodes, _ = await (await get_graph_engine()).get_graph_data()
    entities = {
        match.group(0).lower()
        for _, props in nodes
        for value in (props or {}).values()
        if (match := _ENTITY.search(str(value)))
    }
    return titles, entities


def _document_metadata(document) -> dict:
    """The document's source metadata: in ``system_metadata`` on current cognee,
    in ``external_metadata`` on releases before the move."""
    for field in ("system_metadata", "external_metadata"):
        meta = getattr(document, field, None)
        if isinstance(meta, str):
            meta = json.loads(meta)
        if isinstance(meta, dict) and meta.get("source"):
            return meta
    return {}


def _with_other_project(session):
    session.queue(f"/project/{OTHER}/pipeline", (200, {"items": [OTHER_PIPELINE]}))
    session.queue("/pipeline/other-77/workflow", (200, {"items": []}))
    return session


def test_remember_ingests_new_pipelines_and_updates_finished_ones(cognee_env, session_for):
    dataset = "circleci_incremental"

    async def scenario():
        await _prune()

        await _remember(session_for("index.json"), dataset)
        first = await _memory(dataset)
        await _remember(session_for("index.json", "slow-running/index.json"), dataset)
        running = await _memory(dataset)
        await _remember(session_for("index.json", "slow-finished/index.json"), dataset)
        finished = await _memory(dataset)

        await _prune()
        return first, running, finished

    (first_titles, first_entities), running, finished = asyncio.run(scenario())

    assert len(first_titles) == 5
    assert first_entities == {f"cipipeline{n}" for n in range(1, 6)}
    # #6 appeared while running...
    assert len(running[0]) == 6
    assert running[0][SLOW_RERUN].endswith(": running")
    assert "cipipeline6" in running[1]
    # ...and its document was replaced by the final one, not duplicated.
    assert len(finished[0]) == 6
    assert finished[0][SLOW_RERUN].endswith(": failed")


def test_remember_forgets_a_dropped_project(cognee_env, session_for):
    dataset = "circleci_forget"

    async def scenario():
        await _prune()

        await _remember(_with_other_project(session_for("index.json")), dataset, (SLUG, OTHER))
        before = await _memory(dataset)
        # The fixture project is dropped from project_slugs.
        await _remember(_with_other_project(session_for("index.json")), dataset, (OTHER,))
        after = await _memory(dataset)

        await _prune()
        return before, after

    (before_titles, before_entities), (after_titles, after_entities) = asyncio.run(scenario())

    assert len(before_titles) == 6
    assert before_entities == {f"cipipeline{n}" for n in (1, 2, 3, 4, 5, 77)}
    # The fixture project's pipelines are gone from the dataset and the graph.
    assert list(after_titles) == ["other-77"]
    assert after_entities == {"cipipeline77"}


def test_remember_forgets_the_last_project_of_a_dataset(cognee_env, session_for):
    dataset = "circleci_emptied"

    async def scenario():
        await _prune()

        await _remember(session_for("index.json"), dataset)
        before = await _memory(dataset)
        empty = session_for("index.json")
        empty.queue(f"/project/{OTHER}/pipeline", (200, {"items": []}))
        await _remember(empty, dataset, (OTHER,))
        after = await _memory(dataset)

        await _prune()
        return before, after

    (before_titles, _), (after_titles, after_entities) = asyncio.run(scenario())

    assert len(before_titles) == 5
    assert after_titles == {}
    assert after_entities == set()


def test_remember_keeps_two_sources_apart_in_one_dataset(cognee_env, session_for):
    dataset = "circleci_two_sources"

    async def scenario():
        await _prune()

        await _remember(session_for("index.json"), dataset, resource_name="circleci_a")
        other = _with_other_project(session_for("index.json"))
        await _remember(other, dataset, (OTHER,), resource_name="circleci_b")
        # A second run of each: neither sees the other's project as dropped.
        await _remember(session_for("index.json"), dataset, resource_name="circleci_a")
        other = _with_other_project(session_for("index.json"))
        await _remember(other, dataset, (OTHER,), resource_name="circleci_b")
        after = await _memory(dataset)

        await _prune()
        return after

    titles, entities = asyncio.run(scenario())

    assert len(titles) == 6
    assert entities == {f"cipipeline{n}" for n in (1, 2, 3, 4, 5, 77)}
