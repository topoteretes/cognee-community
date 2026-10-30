"""Shared fixtures for integration tests against a real TypeDB server.

Tests are skipped when no server listens on 127.0.0.1:1729 (or GRAPH_DATABASE_URL).
No LLM or embedding secrets are needed.
"""

import uuid
from types import SimpleNamespace

import pytest
from cognee.infrastructure.databases.provenance import make_source_ref_key
from support import ADDRESS, Concept, server_available

from cognee_community_graph_adapter_typedb import TypeDBAdapter

__all__ = ["ADDRESS", "Concept", "server_available"]


@pytest.fixture
async def adapter():
    """A TypeDBAdapter against a fresh, uniquely named database."""
    if not server_available():
        pytest.skip(f"no TypeDB server at {ADDRESS}")
    adapter = TypeDBAdapter(
        graph_database_url=ADDRESS,
        database_name=f"cognee_test_{uuid.uuid4().hex[:12]}",
    )
    yield adapter
    driver = adapter._get_driver()
    if driver.databases.contains(adapter.database_name):
        driver.databases.get(adapter.database_name).delete()
    await adapter.close()


@pytest.fixture
async def seeded(adapter):
    """The standard test graph: ml -> ai, dl -> ml (is_subset_of), dl -> ai (related_to).

    Every node and edge carries one provenance key (``seeded.key``) attached
    by pipeline run ``seeded.run``.
    """
    dataset_id, data_id, run_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    key = make_source_ref_key(dataset_id, data_id)
    ai = Concept(name="artificial intelligence", description='the "broad" field')
    ml = Concept(name="machine learning")
    dl = Concept(name="deep learning")
    await adapter.add_nodes([ai, ml, dl], source_ref_key=key, pipeline_run_id=str(run_id))
    await adapter.add_edges(
        [
            (str(ml.id), str(ai.id), "is_subset_of", {"weight": 1}),
            (str(dl.id), str(ml.id), "is_subset_of", {}),
            (str(dl.id), str(ai.id), "related_to", None),
        ],
        source_ref_key=key,
        pipeline_run_id=str(run_id),
    )
    return SimpleNamespace(
        adapter=adapter,
        ai=str(ai.id),
        ml=str(ml.id),
        dl=str(dl.id),
        concepts={"ai": ai, "ml": ml, "dl": dl},
        key=key,
        dataset=str(dataset_id),
        run=str(run_id),
    )
