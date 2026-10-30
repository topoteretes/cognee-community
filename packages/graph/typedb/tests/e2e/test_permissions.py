"""Multi-tenant tier: cognee's dataset permissions on top of per-dataset TypeDB databases.

Ports cognee's ``tests/test_delete_permission.py`` to the TypeDB provider with
backend access control on: two users, one dataset owned by the first, and the
graph living in that dataset's own ``cognee_<uuid>`` TypeDB database. Needs
the embedding backend (vectors are written alongside the graph) but no LLM
calls, and is gated with the rest of the e2e tier.
"""

import os
from uuid import UUID, uuid4

import cognee
import pytest
from cognee.api.v1.datasets import datasets
from cognee.context_global_variables import set_database_global_context_variables
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.infrastructure.engine import DataPoint
from cognee.modules.data.exceptions.exceptions import UnauthorizedDataAccessError
from cognee.modules.data.methods import create_authorized_dataset
from cognee.modules.engine.operations.setup import setup
from cognee.modules.pipelines.models import PipelineContext
from cognee.modules.users.methods import create_user
from cognee.modules.users.permissions.methods import authorized_give_permission_on_datasets
from cognee.tasks.storage import add_data_points
from pydantic import BaseModel
from support import ADDRESS, database_exists, server_available

pytestmark = [
    pytest.mark.skipif(os.environ.get("RUN_E2E_TESTS") != "1", reason="set RUN_E2E_TESTS=1"),
    pytest.mark.skipif(not server_available(), reason=f"no TypeDB server at {ADDRESS}"),
    # Vectors are written next to the graph: needs an embedding backend (a
    # hosted one via EMBEDDING_API_KEY, or a local provider such as fastembed).
    pytest.mark.skipif(
        not (os.environ.get("EMBEDDING_API_KEY") or os.environ.get("EMBEDDING_PROVIDER")),
        reason="no embedding backend configured",
    ),
]


class DataItem(BaseModel):
    id: UUID


@pytest.fixture
def access_control_config(typedb_config, isolated_roots, monkeypatch):
    monkeypatch.setenv("ENABLE_BACKEND_ACCESS_CONTROL", "true")


async def test_dataset_permissions_gate_graph_deletes(access_control_config):
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    await setup()

    # Defined here, as in cognee's original: module-level DataPoint subclasses
    # would sit in DataPoint.__subclasses__() for every other test's searches.
    class Organization(DataPoint):
        name: str
        metadata: dict = {"index_fields": ["name"]}

    class Person(DataPoint):
        name: str
        works_for: list[Organization]
        metadata: dict = {"index_fields": ["name"]}

    company_a = Organization(name="Company A")
    company_b = Organization(name="Company B")
    john = Person(name="John", works_for=[company_a, company_b])
    jane = Person(name="Jane", works_for=[company_b])

    owner = await create_user(email=f"owner-{uuid4().hex}@example.com", password="password123")
    other = await create_user(email=f"other-{uuid4().hex}@example.com", password="password123")
    dataset = await create_authorized_dataset(dataset_name="tenant_dataset", user=owner)
    john_item, jane_item = DataItem(id=uuid4()), DataItem(id=uuid4())

    async with set_database_global_context_variables(dataset.id, dataset.owner_id):
        for person, item in ((john, john_item), (jane, jane_item)):
            await add_data_points(
                [person], ctx=PipelineContext(user=owner, dataset=dataset, data_item=item)
            )

    # The dataset's graph lives in its own TypeDB database.
    graph_engine = await get_graph_engine()
    assert graph_engine.database_name == f"cognee_{dataset.id.hex}"
    assert await database_exists(graph_engine.database_name)
    nodes, edges = await graph_engine.get_graph_data()
    assert (len(nodes), len(edges)) == (4, 3)

    # Another user cannot delete from it ...
    with pytest.raises(UnauthorizedDataAccessError):
        await datasets.delete_data(dataset.id, john_item.id, other)
    nodes, edges = await graph_engine.get_graph_data()
    assert (len(nodes), len(edges)) == (4, 3)

    # ... until the owner grants the permission.
    await authorized_give_permission_on_datasets(other.id, [dataset.id], "delete", owner.id)
    await datasets.delete_data(dataset.id, john_item.id, other)
    nodes, edges = await graph_engine.get_graph_data()
    assert (len(nodes), len(edges)) == (2, 1)  # Jane and Company B remain

    # Deleting the last item with delete_dataset_if_empty removes the dataset
    # and, through the handler, its TypeDB database.
    await datasets.delete_data(dataset.id, jane_item.id, other, delete_dataset_if_empty=True)
    assert await graph_engine.get_graph_data() == ([], [])
    assert not await database_exists(graph_engine.database_name)
