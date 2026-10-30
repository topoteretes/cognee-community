import os
import pathlib
from uuid import uuid4

import cognee
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.infrastructure.databases.relational import create_db_and_tables
from cognee.infrastructure.engine import DataPoint

from cognee_community_graph_adapter_memgraph import register

GRAPH_URL = os.getenv("GRAPH_DATABASE_URL", "bolt://localhost:7687")
GRAPH_USERNAME = os.getenv("GRAPH_DATABASE_USERNAME", "memgraph")
GRAPH_PASSWORD = os.getenv("GRAPH_DATABASE_PASSWORD", "memgraph")


class Person(DataPoint):
    name: str


class Project(DataPoint):
    name: str


class Company(DataPoint):
    name: str


async def test_add_edges_persists_all_relationship_types(graph_engine):
    person_id = uuid4()
    project_id = uuid4()
    company_id = uuid4()

    await graph_engine.add_nodes(
        [
            Person(id=person_id, name="Alice"),
            Project(id=project_id, name="Apollo"),
            Company(id=company_id, name="Acme"),
        ]
    )

    edges = [
        (str(person_id), str(project_id), "WORKS_ON", {"role": "developer"}),
        (str(person_id), str(company_id), "EMPLOYED_BY", {"since": "2025"}),
        (str(company_id), str(project_id), "OWNS", {"percentage": "100"}),
    ]

    await graph_engine.add_edges(edges)

    rel_type_rows = await graph_engine.query(
        """
        MATCH ()-[e]->()
        RETURN DISTINCT type(e) AS rel_type
        """
    )
    rel_types = {row["rel_type"] for row in rel_type_rows}
    assert rel_types == {"WORKS_ON", "EMPLOYED_BY", "OWNS"}

    edge_count_rows = await graph_engine.query(
        """
        MATCH ()-[e]->()
        RETURN count(e) AS edge_count
        """
    )
    assert edge_count_rows
    assert edge_count_rows[0]["edge_count"] == 3


async def test_get_graph_metrics_reports_real_counts(graph_engine):
    # Two disconnected components: Alice-Apollo-Acme (3) and Bob-Zeus (2).
    alice_id = uuid4()
    apollo_id = uuid4()
    acme_id = uuid4()
    bob_id = uuid4()
    zeus_id = uuid4()

    await graph_engine.add_nodes(
        [
            Person(id=alice_id, name="Alice"),
            Project(id=apollo_id, name="Apollo"),
            Company(id=acme_id, name="Acme"),
            Person(id=bob_id, name="Bob"),
            Project(id=zeus_id, name="Zeus"),
        ]
    )

    await graph_engine.add_edges(
        [
            (str(alice_id), str(apollo_id), "WORKS_ON", {"role": "developer"}),
            (str(alice_id), str(acme_id), "EMPLOYED_BY", {"since": "2025"}),
            (str(bob_id), str(zeus_id), "WORKS_ON", {"role": "designer"}),
        ]
    )

    metrics = await graph_engine.get_graph_metrics(include_optional=True)

    assert metrics["num_nodes"] == 5
    assert metrics["num_edges"] == 3
    assert metrics["mean_degree"] == (2 * 3) / 5
    assert metrics["edge_density"] == 3 / (5 * 4)
    assert metrics["num_connected_components"] == 2
    assert sorted(metrics["sizes_of_connected_components"], reverse=True) == [3, 2]
    assert metrics["num_selfloops"] == 0


async def main():
    cognee.config.set_relational_db_config({"db_provider": "sqlite"})
    cognee.config.set_graph_database_provider("memgraph")
    register()
    cognee.config.set_graph_db_config(
        {
            "graph_database_url": GRAPH_URL,
            "graph_database_username": GRAPH_USERNAME,
            "graph_database_password": GRAPH_PASSWORD,
        }
    )

    data_directory_path = str(
        pathlib.Path(
            os.path.join(pathlib.Path(__file__).parent, ".data_storage/test_memgraph_edges")
        ).resolve()
    )
    cognee_directory_path = str(
        pathlib.Path(
            os.path.join(pathlib.Path(__file__).parent, ".cognee_system/test_memgraph_edges")
        ).resolve()
    )
    cognee.config.data_root_directory(data_directory_path)
    cognee.config.system_root_directory(cognee_directory_path)

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    await create_db_and_tables()

    graph_engine = await get_graph_engine()
    await graph_engine.delete_graph()

    await test_add_edges_persists_all_relationship_types(graph_engine)

    await graph_engine.delete_graph()
    await test_get_graph_metrics_reports_real_counts(graph_engine)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
