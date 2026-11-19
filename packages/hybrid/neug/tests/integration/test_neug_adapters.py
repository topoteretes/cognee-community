"""NeuG backend adapter smoke tests (graph + vector + shared database).

These run against the embedded NeuG database through the shared connection
manager. They require the ``neug`` Python bindings, which are not a cognee
dependency, so the whole module is skipped when ``neug`` is not importable.

Every test gets a fresh temporary database (via ``NEUG_DB_PATH``) and resets
the process-level connection-manager singleton so tests never share state.
"""

from __future__ import annotations

import uuid

import pytest

neug = pytest.importorskip("neug")

import cognee_community_hybrid_adapter_neug.connection_manager as _cm  # noqa: E402
from cognee.infrastructure.engine import DataPoint  # noqa: E402
from cognee_community_hybrid_adapter_neug.graph_adapter import (  # noqa: E402
    NeuGGraphAdapter,
)
from cognee_community_hybrid_adapter_neug.vector_adapter import (  # noqa: E402
    NeuGVectorAdapter,
)

VECTOR_DIM = 8


@pytest.fixture()
def neug_db(tmp_path, monkeypatch):
    """Point the shared NeuG connection manager at a fresh temporary database."""
    if _cm._manager is not None:
        _cm._manager.shutdown()
    _cm._manager = None
    monkeypatch.setenv("NEUG_DB_PATH", str(tmp_path / "neug_db"))
    yield tmp_path / "neug_db"
    if _cm._manager is not None:
        _cm._manager.shutdown()
    _cm._manager = None


def _vec(text: str) -> list:
    """Deterministic pseudo-embedding so tests never call a real engine."""
    digest = (text * 8).encode()
    return [digest[i] / 255.0 for i in range(VECTOR_DIM)]


class _FakeEmbeddingEngine:
    def get_vector_size(self):
        return VECTOR_DIM

    def get_batch_size(self):
        return 8

    async def embed_text(self, texts):
        return [_vec(t) for t in texts]


class _Chunk(DataPoint):
    text: str
    metadata: dict = {"index_fields": ["text"]}


def _chunk(text: str, tags=None) -> _Chunk:
    return _Chunk(
        id=uuid.uuid5(uuid.NAMESPACE_URL, text),
        text=text,
        belongs_to_set=tags or [],
    )


class _Node:
    """Minimal node stand-in exposing the model_dump() contract."""

    def __init__(self, id, name, type, **extra):
        self.id = id
        self.name = name
        self.type = type
        self.extra = extra

    def model_dump(self):
        return {"id": self.id, "name": self.name, "type": self.type, **self.extra}


# ---------------------------------------------------------------------------
# Graph adapter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graph_crud_and_traversal(neug_db):
    g = NeuGGraphAdapter()
    try:
        await g.add_nodes(
            [
                _Node("n1", "Alice", "Person", age=30),
                _Node("n2", "Bob", "Person", age=25),
                _Node("n3", "Acme", "Entity"),
            ]
        )
        await g.add_edges([("n1", "n2", "knows", {"since": 2020}), ("n2", "n3", "works_at", {})])

        assert await g.is_empty() is False
        assert (await g.get_node("n1"))["name"] == "Alice"
        assert sorted(n["name"] for n in await g.get_nodes(["n1", "n3"])) == ["Acme", "Alice"]
        assert await g.has_edge("n1", "n2", "knows") is True
        assert await g.has_edge("n1", "n3", "knows") is False
        # Contract: return the subset of the input triples that exist.
        assert await g.has_edges([("n1", "n2", "knows"), ("n1", "n3", "knows")]) == [
            ("n1", "n2", "knows")
        ]
        assert [n["id"] for n in await g.get_neighbors("n1")] == ["n2"]

        nodes, edges = await g.get_graph_data()
        assert len(nodes) == 3 and len(edges) == 2

        nodes, edges = await g.get_neighborhood(["n1"], depth=2)
        assert sorted(i for i, _ in nodes) == ["n1", "n2", "n3"]

        # depth=0 means no expansion: just the seed nodes themselves.
        nodes, edges = await g.get_neighborhood(["n1"], depth=0)
        assert [i for i, _ in nodes] == ["n1"]
        assert edges == []

        nodes, _ = await g.get_filtered_graph_data([{"type": ["Person"]}])
        assert sorted(i for i, _ in nodes) == ["n1", "n2"]

        metrics = await g.get_graph_metrics()
        assert metrics["num_nodes"] == 3 and metrics["num_edges"] == 2
    finally:
        await g.close()


@pytest.mark.asyncio
async def test_graph_raw_cypher_and_introspection_shim(neug_db):
    g = NeuGGraphAdapter()
    try:
        await g.add_nodes([_Node("n1", "Alice", "Person"), _Node("n2", "Bob", "Person")])
        await g.add_edge("n1", "n2", "knows")

        rows = await g.query("MATCH (n:Node) WHERE n.type = 'Person' RETURN n.name ORDER BY n.name")
        # Single-column Cypher rows come back unwrapped as scalars.
        assert rows == ["Alice", "Bob"]

        # type() is not a NeuG function; the adapter rewrites it to label().
        rel_types = await g.query("MATCH ()-[r:EDGE]->() RETURN DISTINCT type(r)")
        assert {"EDGE"} == set(rel_types)

        # The schema-introspection shape the NaturalLanguageRetriever relies on.
        introspection = await g.query(
            "MATCH (n) UNWIND keys(n) AS prop RETURN DISTINCT labels(n) AS NodeLabels, "
            "collect(DISTINCT prop) AS Properties"
        )
        assert introspection, "introspection shim returned nothing"

        # Unknown labels (LLM-generated multi-label Cypher) fall back onto the
        # single-table schema instead of erroring out.
        fallback = await g.query("MATCH (e:Person) RETURN e.name ORDER BY e.name")
        assert fallback == ["Alice", "Bob"]

        # Bare-colon relationship patterns ([:Label]) fall back too: the rel
        # label rewrites to EDGE and the node label to Node across rounds.
        bare = await g.query("MATCH ()-[:knows]->(m:Person) RETURN m.name")
        assert bare == ["Bob"]

        # type() inside a string literal must stay untouched; a rewrite would
        # break the query instead of returning zero rows.
        literal_rows = await g.query("MATCH (n:Node) WHERE n.name = 'my type (test)' RETURN n.name")
        assert literal_rows == []
    finally:
        await g.close()


def test_rewrite_missing_label_supports_bare_colon_rel_pattern():
    rewrite = NeuGGraphAdapter._rewrite_missing_label
    bound = rewrite(
        "MATCH (a)-[r:Entity]->(b)",
        "Schema mismatch: Table Entity does not exist (bindRelTableEntries)",
    )
    assert bound == "MATCH (a)-[r:EDGE]->(b)"
    bare = rewrite(
        "MATCH (a)-[:Entity]->(b)",
        "bindRelTableEntries: Table Entity does not exist",
    )
    assert bare == "MATCH (a)-[:EDGE]->(b)"


def test_adapt_cypher_keeps_string_literals():
    from cognee_community_hybrid_adapter_neug.graph_adapter import _adapt_cypher_query

    assert _adapt_cypher_query("RETURN type(n)") == "RETURN label(n)"
    query = "MATCH (n) WHERE n.name = 'my type (test)' RETURN n"
    assert _adapt_cypher_query(query) == query


def test_adapt_cypher_rejects_single_arg_properties():
    """Single-argument properties() crashes the NeuG engine (SIGSEGV) instead
    of raising an error, so the pass-through rejects it up front; the
    documented two-argument path form stays allowed."""
    from cognee_community_hybrid_adapter_neug.graph_adapter import _adapt_cypher_query

    for query in (
        "MATCH (n:Node) RETURN properties(n)",
        "MATCH (a)-[p*1..2]->(c) RETURN PROPERTIES (nodes(p))",
    ):
        with pytest.raises(ValueError, match="single-argument properties"):
            _adapt_cypher_query(query)
    # the documented form passes through untouched
    query = "MATCH (a)-[p*1..2]->(c) RETURN properties(nodes(p), 'name')"
    assert _adapt_cypher_query(query) == query
    # a 'properties(' inside a string literal must not trigger the guard
    query = "MATCH (n) WHERE n.name = 'call properties(x) here' RETURN n"
    assert _adapt_cypher_query(query) == query


@pytest.mark.asyncio
async def test_graph_add_node_string_form(neug_db):
    """The interface contract add_node(str, properties) must work (used e.g.
    by add_model_class_to_graph)."""
    g = NeuGGraphAdapter()
    try:
        await g.add_node("model_cls", {"name": "ModelClass", "type": "DataPoint"})
        node = await g.get_node("model_cls")
        assert node["name"] == "ModelClass"
        assert node["type"] == "DataPoint"
    finally:
        await g.close()


@pytest.mark.asyncio
async def test_graph_delete_paths(neug_db):
    g = NeuGGraphAdapter()
    try:
        await g.add_nodes([_Node("a", "A", "T"), _Node("b", "B", "T")])
        await g.add_edge("a", "b", "rel")
        await g.delete_node("b")
        assert await g.get_node("b") is None
        await g.delete_nodes(["a"])
        assert await g.is_empty() is True

        await g.add_nodes([_Node("x", "X", "T")])
        await g.delete_graph()
        assert await g.is_empty() is True
    finally:
        await g.close()


# ---------------------------------------------------------------------------
# Vector adapter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vector_collection_and_ann_search(neug_db):
    v = NeuGVectorAdapter(embedding_engine=_FakeEmbeddingEngine())
    try:
        assert await v.has_collection("DataPoint_text") is False
        await v.create_collection("DataPoint_text")
        assert await v.has_collection("DataPoint_text") is True

        points = [
            _chunk("the cat sat on the mat", ["set_a"]),
            _chunk("dogs bark at strangers", ["set_b"]),
            _chunk("cats and dogs are pets", ["set_a", "set_b"]),
        ]
        await v.create_data_points("DataPoint_text", points)

        results = await v.search(
            "DataPoint_text",
            query_vector=_vec("the cat sat on the mat"),
            limit=3,
            include_payload=True,
        )
        assert len(results) == 3
        assert results[0].payload["text"] == "the cat sat on the mat"
        scores = [r.score for r in results]
        assert scores == sorted(scores), "ANN results must be lower-is-better ordered"

        # Text-only search embeds the query (LanceDB semantics, e.g. the
        # SUMMARIES retriever relies on it): identical text embeds to the
        # identical vector and must rank first.
        text_hits = await v.search(
            "DataPoint_text",
            query_text="dogs bark at strangers",
            limit=1,
            include_payload=True,
        )
        assert text_hits[0].payload["text"] == "dogs bark at strangers"
    finally:
        await v.close()


@pytest.mark.asyncio
async def test_vector_bm25_and_tag_filters(neug_db):
    v = NeuGVectorAdapter(embedding_engine=_FakeEmbeddingEngine())
    try:
        points = [
            _chunk("the cat sat on the mat", ["set_a"]),
            _chunk("dogs bark at strangers", ["set_b"]),
            _chunk("cats and dogs are pets", ["set_a", "set_b"]),
        ]
        await v.create_data_points("DataPoint_text", points)

        fts = await v.full_text_search("DataPoint_text", "dogs bark", limit=3)
        assert fts[0][0]["text"] == "dogs bark at strangers"

        # BUG-C1 regression: the search pipeline rewrites queries into
        # interrogative form, so an OR-joined FTS query must still return
        # partial matches when chunks lack the question words — and the
        # chunk matching the most query terms ranks first.
        interrogative = await v.full_text_search("DataPoint_text", "What is dogs bark?", limit=3)
        assert interrogative, "interrogative query returned an empty result set"
        assert interrogative[0][0]["text"] == "dogs bark at strangers"

        # P24: bm25 matches exact tokens (no stemming), and the OR tag filter
        # drops bm25 hits outside node_name. "dogs" matches two chunks but
        # "dogs bark at strangers" (set_b only) is filtered out by set_a.
        or_filtered = await v.full_text_search(
            "DataPoint_text", "dogs", limit=5, node_name=["set_a"]
        )
        assert {payload["text"] for payload, _ in or_filtered} == {"cats and dogs are pets"}

        and_filtered = await v.full_text_search(
            "DataPoint_text",
            "pets",
            limit=5,
            node_name=["set_a", "set_b"],
            node_name_filter_operator="AND",
        )
        assert [payload["text"] for payload, _ in and_filtered] == ["cats and dogs are pets"]
    finally:
        await v.close()


@pytest.mark.asyncio
async def test_vector_belongs_to_set_merge(neug_db):
    v = NeuGVectorAdapter(embedding_engine=_FakeEmbeddingEngine())
    try:
        point = _chunk("shared chunk text", ["set_a"])
        await v.create_data_points("DataPoint_text", [point])
        # Re-upsert the same id with a different tag set: tags must accumulate.
        await v.create_data_points("DataPoint_text", [_chunk("shared chunk text", ["set_b"])])

        retrieved = await v.retrieve("DataPoint_text", [str(point.id)])
        merged = retrieved[0].payload.get("belongs_to_set") or []
        assert set(merged) == {"set_a", "set_b"}
    finally:
        await v.close()


@pytest.mark.asyncio
async def test_vector_retrieve_and_delete(neug_db):
    v = NeuGVectorAdapter(embedding_engine=_FakeEmbeddingEngine())
    try:
        points = [_chunk("alpha beta"), _chunk("gamma delta")]
        await v.create_data_points("DataPoint_text", points)

        retrieved = await v.retrieve("DataPoint_text", [str(points[0].id)])
        assert len(retrieved) == 1 and retrieved[0].payload["text"] == "alpha beta"

        await v.delete_data_points("DataPoint_text", [str(points[0].id)])
        assert await v.retrieve("DataPoint_text", [str(points[0].id)]) == []

        await v.delete_collection("DataPoint_text")
        assert await v.has_collection("DataPoint_text") is False
    finally:
        await v.close()


@pytest.mark.asyncio
async def test_vector_long_payload_roundtrip(neug_db):
    """Payload blobs longer than any short VARCHAR default must survive intact."""
    v = NeuGVectorAdapter(embedding_engine=_FakeEmbeddingEngine())
    try:
        long_text = "long haul storage fidelity probe " * 40  # > 1000 chars
        point = _chunk(long_text, ["set_a"])
        await v.create_data_points("DataPoint_text", [point])

        retrieved = await v.retrieve("DataPoint_text", [str(point.id)])
        assert retrieved[0].payload["text"] == long_text
    finally:
        await v.close()


@pytest.mark.asyncio
async def test_vector_payload_survives_database_reopen(neug_db):
    """Data must survive a full database close/reopen cycle (checkpoint + WAL).

    Regression guard for the NeuG 0.2.0 HNSW/checkpoint bug (dialect probe
    P30): a broken close-time checkpoint makes the WAL replay truncate every
    VARCHAR to 255 characters. Closing and reopening the database must leave
    long payloads intact (verified fixed in current builds).
    """
    long_text = "survives the checkpoint cycle " * 40  # > 1000 chars
    point = _chunk(long_text, ["set_a"])

    v = NeuGVectorAdapter(embedding_engine=_FakeEmbeddingEngine())
    await v.create_data_points("DataPoint_text", [point])
    await v.close()  # refcount -> 0: the shared database is really closed

    v2 = NeuGVectorAdapter(embedding_engine=_FakeEmbeddingEngine())
    try:
        retrieved = await v2.retrieve("DataPoint_text", [str(point.id)])
        assert retrieved[0].payload["text"] == long_text
    finally:
        await v2.close()


@pytest.mark.asyncio
async def test_collection_gets_hnsw_index(neug_db):
    """Each collection gets a cosine HNSW index next to the FTS index, built
    eagerly at table creation (``_create_collection_table``) — NeuG 0.2.0 fixed
    the upsert/index-capacity crash that previously forced deferring the build
    to first search. SHOW INDEXES is not part of the dialect, so index presence
    is asserted by attempting the same CREATE INDEX the adapter runs."""
    v = NeuGVectorAdapter(embedding_engine=_FakeEmbeddingEngine())
    try:
        await v.create_data_points("DataPoint_text", [_chunk("indexed text", ["set_a"])])
        table = await v.get_collection("DataPoint_text")
        # Indexes are built eagerly at table creation, so re-creating the
        # adapter's own HNSW index must already say "already exists" without
        # any search running first.
        with pytest.raises(RuntimeError, match="already exists"):
            await v.connection_manager.execute(
                f"CREATE INDEX {table}_hnsw_idx "
                f"ON {table} USING HNSW (vector) WITH (metric = 'cosine')"
            )
        # ANN hits the eagerly-built index from the first row (no lazy build).
        hits = await v.search(
            "DataPoint_text", query_vector=_vec("indexed text"), limit=1, include_payload=True
        )
        assert hits and hits[0].payload["text"] == "indexed text"
    finally:
        await v.close()


def test_sanitize_fts_query_quotes_tokens():
    """FTS5 reserved words (AND/OR/NOT) and specials must stay literals,
    and tokens are OR-joined so partial matches rank via bm25 instead of
    being excluded by the FTS5 default AND."""
    sanitize = NeuGVectorAdapter._sanitize_fts_query
    assert sanitize("why is it not working?") == '"why" OR "is" OR "it" OR "not" OR "working"'
    assert sanitize("or") == '"or"'
    assert sanitize('say "hi"') == '"say" OR "hi"'


def test_node_name_where_escapes_regex_metacharacters():
    # NeuG CONTAINS parses the literal as a regex: '.' must not match any char.
    # The regex backslash is doubled because the Cypher literal parser only
    # accepts ``\'`` / ``\\`` escape sequences (``\.`` fails to parse).
    where = NeuGVectorAdapter._node_name_where(NeuGVectorAdapter, ["set.a"], "OR")
    assert where == "(v.belongs_to_set CONTAINS '#set\\\\.a#')"
    # Single quotes use ``\'`` (SQL-style ``''`` doubling is a parse error).
    where = NeuGVectorAdapter._node_name_where(NeuGVectorAdapter, ["o'brien"], "OR")
    assert where == "(v.belongs_to_set CONTAINS '#o\\'brien#')"
    with pytest.raises(ValueError):
        NeuGVectorAdapter._node_name_where(NeuGVectorAdapter, ["a#b"], "OR")


@pytest.mark.asyncio
async def test_vector_stale_table_is_recreated(neug_db):
    """A collection table left behind by an older (short VARCHAR) DDL must be
    detected and recreated instead of silently truncating payloads: NeuG's
    ``CREATE NODE TABLE IF NOT EXISTS`` never upgrades an existing schema."""
    from cognee_community_hybrid_adapter_neug.connection_manager import get_neug_connection_manager

    cm = get_neug_connection_manager()
    cm.acquire()
    await cm.execute(
        "CREATE NODE TABLE DataPoint_text("
        "id STRING PRIMARY KEY, vector FLOAT[8], text VARCHAR(255), "
        "payload VARCHAR(255), belongs_to_set VARCHAR(255))"
    )
    cm.release()

    v = NeuGVectorAdapter(embedding_engine=_FakeEmbeddingEngine())
    try:
        long_text = "stale schema recreation probe " * 30  # ~900 chars
        point = _chunk(long_text)
        await v.create_data_points("DataPoint_text", [point])

        retrieved = await v.retrieve("DataPoint_text", [str(point.id)])
        assert retrieved[0].payload["text"] == long_text
    finally:
        await v.close()


@pytest.mark.asyncio
async def test_vector_prune_discovers_cross_instance_collections(neug_db):
    """prune() in a FRESH adapter instance must still drop collections that
    an earlier instance persisted (registry-based discovery, not the empty
    in-memory cache)."""
    v = NeuGVectorAdapter(embedding_engine=_FakeEmbeddingEngine())
    await v.create_data_points("DataPoint_text", [_chunk("prune discovery probe")])
    await v.close()

    v2 = NeuGVectorAdapter(embedding_engine=_FakeEmbeddingEngine())
    g = NeuGGraphAdapter()
    try:
        await g.add_nodes([_Node("n1", "Alice", "Person")])
        assert v2._collections == {}
        await v2.prune()
        assert await v2.has_collection("DataPoint_text") is False
        # The graph tables in the same database are untouched.
        assert (await g.get_node("n1"))["name"] == "Alice"
    finally:
        await v2.close()
        await g.close()


def test_connection_manager_survives_new_event_loop(neug_db):
    """The process-level singleton outlives individual event loops: each
    asyncio.run() gets a fresh loop, so the statement lock must be rebound
    instead of raising 'bound to a different event loop'."""
    import asyncio

    async def _is_empty():
        adapter = NeuGGraphAdapter()
        try:
            return await adapter.is_empty()
        finally:
            await adapter.close()

    assert asyncio.run(_is_empty()) is True
    assert asyncio.run(_is_empty()) is True


# ---------------------------------------------------------------------------
# Shared database: graph + vector adapters coexist on one NeuG DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shared_database_coexistence(neug_db):
    v = NeuGVectorAdapter(embedding_engine=_FakeEmbeddingEngine())
    g = NeuGGraphAdapter()
    try:
        await v.create_data_points("DataPoint_text", [_chunk("the cat sat on the mat")])
        await g.add_nodes([_Node("n1", "Alice", "Person")])

        # Both channels serve reads while the other adapter holds a reference.
        assert (await g.get_node("n1"))["name"] == "Alice"
        hits = await v.full_text_search("DataPoint_text", "cat", limit=5)
        assert hits and hits[0][0]["text"] == "the cat sat on the mat"

        # Closing the graph adapter must not tear the shared connection down.
        await g.close()
        still = await v.full_text_search("DataPoint_text", "cat", limit=5)
        assert still, "vector search broke after graph adapter close"

        # Vector prune drops collection tables (registry-based) but must not
        # touch Node/EDGE.
        await v.prune()
        assert await v.has_collection("DataPoint_text") is False
    finally:
        await v.close()

    # A fresh graph handle confirms the graph tables survived the vector prune.
    g2 = NeuGGraphAdapter()
    try:
        assert await g2.is_empty() is False, "vector prune wiped graph tables"
        assert (await g2.get_node("n1"))["name"] == "Alice"
    finally:
        await g2.close()
