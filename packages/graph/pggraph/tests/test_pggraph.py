"""Tests for the pgGraph community adapter."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from cognee.infrastructure.databases.graph.postgres.adapter import PostgresAdapter

from cognee_community_graph_adapter_pggraph import PgGraphAdapter, register
from cognee_community_graph_adapter_pggraph.connection import (
    _normalize_scheme,
    resolve_connection_string,
)

PGGRAPH_IT = os.getenv("COGNEE_PGGRAPH_TESTS", "").lower() in ("1", "true", "yes")


def _connection_string() -> str:
    return resolve_connection_string(
        graph_database_host=os.getenv("GRAPH_DATABASE_HOST", os.getenv("DB_HOST", "localhost")),
        graph_database_port=os.getenv("GRAPH_DATABASE_PORT", os.getenv("DB_PORT", "5433")),
        graph_database_name=os.getenv("GRAPH_DATABASE_NAME", os.getenv("DB_NAME", "cognee")),
        graph_database_username=os.getenv(
            "GRAPH_DATABASE_USERNAME", os.getenv("DB_USERNAME", "cognee")
        ),
        graph_database_password=os.getenv(
            "GRAPH_DATABASE_PASSWORD", os.getenv("DB_PASSWORD", "cognee")
        ),
    )


@pytest_asyncio.fixture
async def adapter():
    register()
    inst = PgGraphAdapter(connection_string=_connection_string())
    await inst.initialize()
    yield inst
    await inst.delete_graph()


@pytest.mark.asyncio
async def test_register_adds_pggraph_provider():
    register()
    from cognee.infrastructure.databases.graph.supported_databases import supported_databases

    assert supported_databases["pggraph"] is PgGraphAdapter


@pytest.mark.asyncio
async def test_falls_back_when_pggraph_traversal_fails():
    adapter = PgGraphAdapter.__new__(PgGraphAdapter)
    adapter._pggraph_ready = True

    with patch.object(
        adapter,
        "_pggraph_get_neighbors",
        AsyncMock(side_effect=RuntimeError("pgGraph unavailable")),
    ):
        with patch.object(
            PostgresAdapter,
            "get_neighbors",
            AsyncMock(return_value=[{"id": "b", "name": "B", "type": "Entity"}]),
        ) as postgres_neighbors:
            neighbors = await PgGraphAdapter.get_neighbors(adapter, "a")

    postgres_neighbors.assert_awaited_once_with("a")
    assert neighbors[0]["id"] == "b"


class TestConnectionString:
    """The connection string helper must produce URLs that ``create_async_engine`` accepts."""

    def test_postgresql_scheme_is_rewritten_to_asyncpg(self):
        url = _normalize_scheme("postgresql://user:pass@host:5432/db")
        assert url == "postgresql+asyncpg://user:pass@host:5432/db"

    def test_legacy_postgres_scheme_is_rewritten_to_asyncpg(self):
        url = _normalize_scheme("postgres://user:pass@host:5432/db")
        assert url == "postgresql+asyncpg://user:pass@host:5432/db"

    def test_asyncpg_scheme_is_left_alone(self):
        url = "postgresql+asyncpg://user:pass@host:5432/db"
        assert _normalize_scheme(url) == url

    def test_explicit_psycopg_scheme_is_preserved(self):
        # If the user explicitly chose a different driver, don't override it;
        # SQLAlchemy will raise a clear error if it's not async-capable.
        url = "postgresql+psycopg://user:pass@host:5432/db"
        assert _normalize_scheme(url) == url

    def test_resolver_passes_through_normalized_url(self):
        out = resolve_connection_string(graph_database_url="postgresql://user:pass@host:5432/db")
        assert out == "postgresql+asyncpg://user:pass@host:5432/db"

    def test_resolver_passes_through_legacy_url(self):
        out = resolve_connection_string(graph_database_url="postgres://user:pass@host:5432/db")
        assert out == "postgresql+asyncpg://user:pass@host:5432/db"

    def test_resolver_builds_url_from_parts_with_async_driver(self):
        out = resolve_connection_string(
            graph_database_host="db.example",
            graph_database_port="5432",
            graph_database_name="cognee",
            graph_database_username="u",
            graph_database_password="p",
        )
        assert out == "postgresql+asyncpg://u:p@db.example:5432/cognee"


class TestFactoryShapedConstruction:
    """cognee's ``_create_graph_engine`` factory calls registered adapters with
    ``database_name=...`` (not ``graph_database_name=...``) and doesn't pass a
    host parameter. The adapter must still be able to construct a working
    connection string from these inputs.
    """

    def test_database_name_kwarg_is_accepted_as_graph_database_name(self):
        captured = {}

        def fake_resolver(**kwargs):
            captured.update(kwargs)
            return "postgresql+asyncpg://u:p@h:5432/cognee"

        with patch(
            "cognee_community_graph_adapter_pggraph.pggraph_adapter.resolve_connection_string",
            side_effect=fake_resolver,
        ):
            with patch.object(PostgresAdapter, "__init__", return_value=None):
                PgGraphAdapter(
                    graph_database_url="",
                    graph_database_username="u",
                    graph_database_password="p",
                    graph_database_port="5432",
                    graph_database_key="",
                    database_name="cognee",
                    graph_database_host="h",
                )

        assert captured["graph_database_name"] == "cognee"
        assert captured["graph_database_host"] == "h"
        assert captured["graph_database_username"] == "u"

    def test_explicit_graph_database_name_takes_priority_over_database_name(self):
        captured = {}

        def fake_resolver(**kwargs):
            captured.update(kwargs)
            return "postgresql+asyncpg://u:p@h:5432/explicit"

        with patch(
            "cognee_community_graph_adapter_pggraph.pggraph_adapter.resolve_connection_string",
            side_effect=fake_resolver,
        ):
            with patch.object(PostgresAdapter, "__init__", return_value=None):
                PgGraphAdapter(
                    graph_database_name="explicit",
                    database_name="from_factory",
                    graph_database_host="h",
                    graph_database_port="5432",
                    graph_database_username="u",
                    graph_database_password="p",
                )

        assert captured["graph_database_name"] == "explicit"


class TestStickyFallback:
    """Once pgGraph fails, the adapter should stop retrying it for the rest of
    this instance's lifetime — otherwise every call double-queries and logs.
    """

    @pytest.mark.asyncio
    async def test_get_neighbors_disables_pggraph_after_failure(self):
        adapter = PgGraphAdapter.__new__(PgGraphAdapter)
        adapter._pggraph_ready = True

        pg_neighbors = AsyncMock(side_effect=RuntimeError("pgGraph borked"))
        postgres_neighbors = AsyncMock(return_value=[])

        with patch.object(adapter, "_pggraph_get_neighbors", pg_neighbors):
            with patch.object(PostgresAdapter, "get_neighbors", postgres_neighbors):
                await PgGraphAdapter.get_neighbors(adapter, "a")
                assert adapter._pggraph_ready is False
                await PgGraphAdapter.get_neighbors(adapter, "b")

        # pgGraph attempted only on the first call, not the second.
        assert pg_neighbors.await_count == 1
        assert postgres_neighbors.await_count == 2

    @pytest.mark.asyncio
    async def test_get_neighborhood_disables_pggraph_after_failure(self):
        adapter = PgGraphAdapter.__new__(PgGraphAdapter)
        adapter._pggraph_ready = True

        pg_nbhd = AsyncMock(side_effect=RuntimeError("traverse exploded"))
        sql_nbhd = AsyncMock(return_value=([], []))

        with patch.object(adapter, "_pggraph_get_neighborhood", pg_nbhd):
            with patch.object(adapter, "_postgres_get_neighborhood", sql_nbhd):
                await PgGraphAdapter.get_neighborhood(adapter, ["a"], depth=2)
                assert adapter._pggraph_ready is False
                await PgGraphAdapter.get_neighborhood(adapter, ["b"], depth=2)

        assert pg_nbhd.await_count == 1
        assert sql_nbhd.await_count == 2


class TestSeedInclusionInNeighborhood:
    """``graph.traverse()`` may omit seeds with no outgoing edges. The adapter
    must still return those seeds so its behavior matches the SQL fallback.
    """

    @pytest.mark.asyncio
    async def test_isolated_seeds_are_included_when_traverse_returns_nothing(self):
        adapter = PgGraphAdapter.__new__(PgGraphAdapter)
        adapter._pggraph_ready = True

        empty_result = MagicMock()
        empty_result.fetchall.return_value = []

        fake_session = MagicMock()
        fake_session.execute = AsyncMock(return_value=empty_result)

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_session_cm():
            yield fake_session

        captured_subgraph_ids: list = []

        async def fake_fetch_subgraph(node_ids, edge_types=None):
            captured_subgraph_ids.extend(node_ids)
            return [(nid, {"name": nid, "type": "Entity"}) for nid in node_ids], []

        with patch.object(adapter, "_session", fake_session_cm):
            with patch.object(adapter, "_fetch_subgraph_for_ids", fake_fetch_subgraph):
                nodes, edges = await PgGraphAdapter._pggraph_get_neighborhood(
                    adapter, ["seed-1", "seed-2"], depth=2, edge_types=None
                )

        # Both isolated seeds must appear in the subgraph lookup.
        assert set(captured_subgraph_ids) == {"seed-1", "seed-2"}
        assert {nid for nid, _ in nodes} == {"seed-1", "seed-2"}
        assert edges == []


@pytest.mark.skipif(not PGGRAPH_IT, reason="Set COGNEE_PGGRAPH_TESTS=1 with Postgres running")
@pytest.mark.asyncio
async def test_factory_shaped_construction_connects_to_real_postgres():
    """End-to-end check that the factory call shape (``database_name``, no
    ``connection_string``) actually yields a working adapter against a real
    Postgres. This is the path that ``cognee._create_graph_engine`` takes."""
    register()
    inst = PgGraphAdapter(
        graph_database_url="",
        graph_database_username=os.getenv(
            "GRAPH_DATABASE_USERNAME", os.getenv("DB_USERNAME", "cognee")
        ),
        graph_database_password=os.getenv(
            "GRAPH_DATABASE_PASSWORD", os.getenv("DB_PASSWORD", "cognee")
        ),
        graph_database_port=os.getenv("GRAPH_DATABASE_PORT", os.getenv("DB_PORT", "5433")),
        graph_database_host=os.getenv("GRAPH_DATABASE_HOST", os.getenv("DB_HOST", "localhost")),
        # cognee's factory passes the database name under this key.
        database_name=os.getenv("GRAPH_DATABASE_NAME", os.getenv("DB_NAME", "cognee")),
        graph_database_key="",
    )
    try:
        await inst.initialize()
        assert await inst.is_empty() in (True, False)
    finally:
        await inst.delete_graph()


@pytest.mark.skipif(not PGGRAPH_IT, reason="Set COGNEE_PGGRAPH_TESTS=1 with Postgres running")
@pytest.mark.asyncio
async def test_adapter_seed_and_traverse(adapter):
    await adapter.add_nodes(
        [
            ("a", {"name": "A", "type": "Entity"}),
            ("b", {"name": "B", "type": "Entity"}),
        ]
    )
    await adapter.add_edges([("a", "b", "relates_to", {})])

    neighbors = await adapter.get_neighbors("a")
    assert len(neighbors) == 1
    assert neighbors[0]["id"] == "b"

    nodes, edges = await adapter.get_neighborhood(["a"], depth=2)
    assert len(nodes) >= 2
    assert len(edges) >= 1
