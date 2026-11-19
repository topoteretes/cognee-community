"""Forget-on-delete must remove a model's cognified graph content, not just
its Data record. Syncs a manifest with two models, cognifies (LLM/embeddings
mocked, no live creds), removes one model from the project and re-syncs via a
new successful run, and asserts the removed model's extracted entity is gone
from the graph while the surviving model's entity remains.
"""

import importlib
import json
import re
from urllib.parse import urlsplit

import cognee
import pytest
import pytest_asyncio
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.infrastructure.databases.vector.embeddings.LiteLLMEmbeddingEngine import (
    LiteLLMEmbeddingEngine,
)
from cognee.infrastructure.llm import LLMGateway

from cognee_community_connector_dbt_cloud import dbt_cloud_source

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")

DATASET = "dbt_cloud_forget_test"
ACCOUNT_ID = 1
ENVIRONMENT_ID = 10
# Distinctive, unique tokens so each model maps to exactly one graph entity.
ALPHA = "Alphacorp"
BRAVO = "Bravocorp"


def _manifest(nodes):
    return {
        "metadata": {
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
            "project_name": "jaffle_shop",
        },
        "nodes": nodes,
        "sources": {},
        "exposures": {},
        "metrics": {},
        "parent_map": {},
        "child_map": {},
    }


def _model_node(name, description):
    return {
        "name": name,
        "resource_type": "model",
        "package_name": "jaffle_shop",
        "description": description,
        "config": {"materialized": "view"},
        "database": "db",
        "schema": "main",
        "alias": name,
        "original_file_path": f"models/{name}.sql",
        "tags": [],
        "meta": {},
        "columns": {},
        "raw_code": "",
    }


class _FakeResponse:
    def __init__(self, status_code, payload=None, raw_bytes=None):
        self.status_code = status_code
        self._payload = payload
        self._raw_bytes = raw_bytes

    def json(self):
        return self._payload

    def iter_content(self, chunk_size=65536):
        data = (
            self._raw_bytes if self._raw_bytes is not None else json.dumps(self._payload).encode()
        )
        for i in range(0, len(data), chunk_size):
            yield data[i : i + chunk_size]


def _envelope(data):
    return {"data": data, "extra": {}, "status": {"is_success": True}}


class FakeDbtCloudSession:
    """Minimal fake covering only what this forget test exercises: one job,
    one successful run per sync, and that run's manifest. See
    ``tests/test_dbt_cloud.py``'s ``FakeDbtCloudSession`` for the fuller fake
    used by the rest of the connector's test suite.
    """

    def __init__(self, job, runs, manifests):
        self.job = job
        self.runs = runs
        self.manifests = manifests

    def get(self, url, params=None, stream=False):
        path = urlsplit(url).path
        query = dict(params or {})

        if re.fullmatch(r"/api/v2/accounts/\d+/jobs/", path):
            offset = int(query.get("offset", 0))
            items = [self.job][offset : offset + 100]
            return _FakeResponse(
                200,
                {
                    "data": items,
                    "extra": {"pagination": {"count": len(items), "total_count": 1}},
                    "status": {"is_success": True},
                },
            )

        m = re.fullmatch(r"/api/v2/accounts/\d+/runs/(\d+)/", path)
        if m:
            run = self.runs[int(m.group(1))]
            data = dict(run)
            if "run_steps" in (query.get("include_related") or ""):
                data["run_steps"] = []
            return _FakeResponse(200, _envelope(data))

        if re.fullmatch(r"/api/v2/accounts/\d+/runs/", path):
            items = [r for r in self.runs.values() if r["job_definition_id"] == self.job["id"]]
            items = sorted(items, key=lambda r: r["finished_at"], reverse=True)
            return _FakeResponse(
                200,
                {
                    "data": items,
                    "extra": {"pagination": {"count": len(items), "total_count": len(items)}},
                    "status": {"is_success": True},
                },
            )

        m = re.fullmatch(r"/api/v2/accounts/\d+/runs/(\d+)/artifacts/manifest\.json", path)
        if m:
            return _FakeResponse(
                200, raw_bytes=json.dumps(self.manifests[int(m.group(1))]).encode()
            )

        return _FakeResponse(404)


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
        name = next((t for t in (ALPHA, BRAVO) if text_input and t in text_input), None)
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
    for _nid, props in nodes:
        if any(token in str(v).lower() for v in (props or {}).values()):
            return True
    return False


@pytest_asyncio.fixture
async def clean_environment(tmp_path, monkeypatch):
    pytest.importorskip("dlt")
    pytest.importorskip("ladybug")

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
        dbt_cloud_source(
            account_id=ACCOUNT_ID,
            environment_ids=[ENVIRONMENT_ID],
            api_token="tok",
            session=session,
        ),
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    await cognee.cognify(datasets=[DATASET])


@pytest.mark.asyncio
async def test_removing_a_model_forgets_its_graph_content(clean_environment):
    job = {
        "id": 1,
        "name": "Job",
        "environment_id": ENVIRONMENT_ID,
        "project_id": 100,
        "job_type": "scheduled",
        "is_system": False,
        "state": 1,
    }

    session1 = FakeDbtCloudSession(
        job=job,
        runs={
            1: {
                "id": 1,
                "job_definition_id": 1,
                "environment_id": ENVIRONMENT_ID,
                "project_id": 100,
                "status": 10,
                "finished_at": "2024-01-01T00:00:00+00:00",
                "git_branch": "main",
                "git_sha": "abc123",
                "status_message": None,
            }
        },
        manifests={
            1: _manifest(
                {
                    "model.jaffle_shop.orders": _model_node(
                        "orders", f"{ALPHA} is a company in the logistics sector."
                    ),
                    "model.jaffle_shop.customers": _model_node(
                        "customers", f"{BRAVO} is an unrelated company in the finance sector."
                    ),
                }
            )
        },
    )
    await _sync(session1)
    assert await _graph_has(ALPHA), "orders model entity should be in the graph after ingest"
    assert await _graph_has(BRAVO), "customers model entity should be in the graph after ingest"

    # A new successful run whose manifest no longer has the customers model
    # (removed from the dbt project) -- the incremental re-sync must forget it.
    session2 = FakeDbtCloudSession(
        job=job,
        runs={
            1: session1.runs[1],
            2: {
                **session1.runs[1],
                "id": 2,
                "finished_at": "2024-01-02T00:00:00+00:00",
            },
        },
        manifests={
            2: _manifest(
                {
                    "model.jaffle_shop.orders": _model_node(
                        "orders", f"{ALPHA} is a company in the logistics sector."
                    ),
                }
            )
        },
    )
    await _sync(session2)

    assert await _graph_has(ALPHA), "surviving orders model entity must remain"
    assert not await _graph_has(BRAVO), (
        "removed customers model's entity must be removed from the graph "
        "(forget-on-delete), not just its Data record"
    )
