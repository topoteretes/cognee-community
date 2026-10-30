"""The pure edge-spec builder: which rows link to which, from the source's own rows."""

from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cognee_community_connector_gitgraph import build_sample_repo, gitgraph_source, links
from cognee_community_connector_gitgraph.links import (
    build_edges,
    graph_tables,
    index_rows,
    link_rows,
)

FIXTURES = Path(__file__).parent / "fixtures" / "ci"

IN_COMMIT = "sha_references_commits"
AT_COMMIT = "commit_sha_references_commits"
FROM_SYMBOL = "from_symbol_references_symbols"
TO_SYMBOL = "to_symbol_references_symbols"

Tables = dict[str, list[dict[str, Any]]]


@pytest.fixture
def tables(tmp_path: Path) -> Tables:
    source = gitgraph_source(build_sample_repo(tmp_path / "repo").path, ci_logs=[FIXTURES])
    return {name: list(resource) for name, resource in source.resources.items()}


def _counts(tables: Tables) -> Counter[str]:
    return Counter(relationship for _, _, relationship in build_edges(tables))


def test_sample_edge_counts(tables: Tables) -> None:
    # 15 file changes; 10 references, 5 of them calls made inside a symbol (the imports have no
    # owner); the 2 CI failures name no commit that exists (one has no sha, one an unknown sha).
    assert _counts(tables) == {IN_COMMIT: 15, FROM_SYMBOL: 5, TO_SYMBOL: 10}


def test_edges_are_sorted_and_deterministic(tables: Tables) -> None:
    edges = build_edges(tables)
    assert edges == sorted(set(edges))
    shuffled = {name: list(reversed(rows)) for name, rows in tables.items()}
    assert build_edges(shuffled) == edges


def test_every_edge_ends_at_an_existing_row(tables: Tables) -> None:
    keys = {(name, row["id"]) for name, rows in tables.items() for row in rows}
    for source, target, _ in build_edges(tables):
        assert source in keys
        assert target in keys


def test_null_from_symbol_gives_no_from_edge(tables: Tables) -> None:
    imports = {r["id"] for r in tables["references"] if r["from_symbol"] is None}
    assert len(imports) == 5
    from_sources = {s[1] for s, _, rel in build_edges(tables) if rel == FROM_SYMBOL}
    assert not imports & from_sources
    to_sources = {s[1] for s, _, rel in build_edges(tables) if rel == TO_SYMBOL}
    assert imports <= to_sources


def test_edges_use_row_keys_of_table_and_id(tables: Tables) -> None:
    edges = build_edges(tables)
    sha = tables["file_changes"][0]["sha"]
    row = tables["file_changes"][0]["id"]
    assert (("file_changes", row), ("commits", sha), IN_COMMIT) in edges
    call = next(r for r in tables["references"] if r["from_symbol"] == "billing/invoice.py::charge")
    assert (("references", call["id"]), ("symbols", "billing/invoice.py::charge"), FROM_SYMBOL) in (
        edges
    )


def test_dangling_targets_make_no_edges(tables: Tables) -> None:
    tables["file_changes"].append({"id": "dead:x.py", "sha": "dead" * 10})
    tables["references"].append(
        {"id": "r1", "from_symbol": "nope::a", "to_symbol": "nope::b", "kind": "call"}
    )
    assert _counts(tables) == {IN_COMMIT: 15, FROM_SYMBOL: 5, TO_SYMBOL: 10}


def test_duplicate_rows_give_one_edge(tables: Tables) -> None:
    tables["file_changes"] += tables["file_changes"]
    assert _counts(tables) == {IN_COMMIT: 15, FROM_SYMBOL: 5, TO_SYMBOL: 10}


def _ci(commit_sha: str | None, row_id: str = "job:t") -> dict[str, Any]:
    return {"id": row_id, "commit_sha": commit_sha}


def test_ci_failure_links_by_full_sha_and_unique_prefix(tables: Tables) -> None:
    sha = tables["commits"][0]["id"]
    tables["ci_failures"] = [_ci(sha, "a"), _ci(sha[:7], "b"), _ci(None, "c"), _ci("0" * 7, "d")]
    at = [(s[1], t[1]) for s, t, rel in build_edges(tables) if rel == AT_COMMIT]
    assert at == [("a", sha), ("b", sha)]


def test_ambiguous_prefix_makes_no_edge() -> None:
    commits = [{"id": "abcdef1" + "0" * 33}, {"id": "abcdef1" + "1" * 33}]
    tables: Tables = {"commits": commits, "ci_failures": [_ci("abcdef1"), _ci("abcdef10")]}
    at = [(s[1], t[1]) for s, t, rel in build_edges(tables) if rel == AT_COMMIT]
    assert at == [("job:t", commits[0]["id"])]


def _graph(*rows: tuple[str, str, str]) -> tuple[list[Any], list[Any]]:
    """Fake graph data: one table node per table and a row node per (node id, table, text)."""
    tables = sorted({table for _, table, _ in rows})
    nodes: list[Any] = [(f"t-{t}", {"name": t}) for t in tables]
    nodes += [(node, {"text": text}) for node, _, text in rows]
    edges: list[Any] = [(node, f"t-{table}", "is_row_of", {}) for node, table, _ in rows]
    return nodes, edges


def _text(table: str, *lines: str) -> str:
    return f"Table: {table}\nColumns:\n  - id (TEXT)\n\nRow Data:\n" + "\n".join(lines)


def test_row_index_maps_table_and_id_to_node() -> None:
    nodes, edges = _graph(("n1", "commits", _text("commits", "  id: abc", "  message: hi")))
    assert index_rows(nodes, edges) == ({("commits", "abc"): "n1"}, set())


def test_row_text_without_a_schema_block_is_read() -> None:
    # cognee 1.6.6 writes only the table line before Row Data (no Columns, no blank line).
    real = "Table: commits\nRow Data:\n  id: abc\n  message: hi"
    forged = "Table: commits\nRow Data:\n  id: ccc\n  message: x\nRow Data:\n  id: aaa"
    other = "Table: commitsx\nRow Data:\n  id: zzz"
    nodes, edges = _graph(
        ("n1", "commits", real), ("n2", "commits", forged), ("n3", "commits", other)
    )
    assert index_rows(nodes, edges) == ({("commits", "abc"): "n1", ("commits", "ccc"): "n2"}, set())


def test_spoofed_id_line_in_a_later_column_is_ignored() -> None:
    real = _text("commits", "  id: aaa", "  message: x")
    spoof = _text("commits", "  id: bbb", "  message: x\n  id: aaa\n  y: z")
    # the spoof also tries to fake the header
    forged = _text("commits", "  id: ccc", "  message: x\nRow Data:\n  id: aaa")
    nodes, edges = _graph(
        ("n1", "commits", real), ("n2", "commits", spoof), ("n3", "commits", forged)
    )
    found, ambiguous = index_rows(nodes, edges)
    assert found == {("commits", "aaa"): "n1", ("commits", "bbb"): "n2", ("commits", "ccc"): "n3"}
    assert not ambiguous


def test_row_whose_id_line_is_not_first_is_ignored() -> None:
    late = _text("commits", "  message: x\n  id: aaa", "  id: zzz")
    nodes, edges = _graph(("n1", "commits", late))
    assert index_rows(nodes, edges) == ({}, set())


def test_duplicate_key_on_different_nodes_is_refused() -> None:
    text = _text("commits", "  id: aaa")
    nodes, edges = _graph(("n1", "commits", text), ("n2", "commits", text))
    assert index_rows(nodes, edges) == ({}, {("commits", "aaa")})


def test_same_id_in_different_tables_is_fine() -> None:
    nodes, edges = _graph(
        ("n1", "commits", _text("commits", "  id: a")),
        ("n2", "symbols", _text("symbols", "  id: a")),
    )
    found, ambiguous = index_rows(nodes, edges)
    assert found == {("commits", "a"): "n1", ("symbols", "a"): "n2"}
    assert not ambiguous


def test_text_for_another_table_is_ignored() -> None:
    nodes, edges = _graph(("n1", "commits", _text("symbols", "  id: a")))
    assert index_rows(nodes, edges) == ({}, set())


async def test_link_rows_skips_ambiguous_and_missing_rows(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    sha_a, sha_b, sha_c = "a" * 40, "b" * 40, "c" * 40
    nodes, edges = _graph(
        ("c1", "commits", _text("commits", f"  id: {sha_a}")),
        ("c2", "commits", _text("commits", f"  id: {sha_a}")),  # duplicate of sha_a
        ("c3", "commits", _text("commits", f"  id: {sha_b}")),
        ("f1", "file_changes", _text("file_changes", f"  id: {sha_a}:x")),
        ("f2", "file_changes", _text("file_changes", f"  id: {sha_b}:x")),
    )
    added: list[Any] = []

    class Engine:
        async def get_graph_data(self) -> tuple[list[Any], list[Any]]:
            return nodes, edges

        async def add_edges(self, batch: list[Any]) -> None:
            added.extend(batch)

    async def engine() -> Engine:
        return Engine()

    monkeypatch.setattr(links, "get_graph_engine", engine)
    tables = {
        "commits": [{"id": s} for s in (sha_a, sha_b, sha_c)],
        "file_changes": [
            {"id": f"{sha_a}:x", "sha": sha_a},
            {"id": f"{sha_b}:x", "sha": sha_b},
            {"id": f"{sha_c}:x", "sha": sha_c},
        ],
    }

    with caplog.at_level("WARNING"):
        summary = await link_rows(SimpleNamespace(resources=tables))

    assert summary == {"sha_references_commits": 1, "missing": 1, "ambiguous": 1}
    assert [(a, b) for a, b, _, _ in added] == [("f2", "c3")]
    assert "ambiguous" in caplog.text


def test_missing_tables_are_fine() -> None:
    assert build_edges({}) == []
    assert build_edges({"commits": [{"id": "a" * 40}]}) == []


def _linked(tables: Tables) -> tuple[list[Any], list[Any]]:
    """Fake graph data as cognee holds it after ``link_rows``: one node per row, keyed by id."""
    nodes, edges = _graph(
        *[(f"{t}/{r['id']}", t, _text(t, f"  id: {r['id']}")) for t in tables for r in tables[t]]
    )
    edges += [(f"{a[0]}/{a[1]}", f"{b[0]}/{b[1]}", rel, {}) for a, b, rel in build_edges(tables)]
    return nodes, edges


def test_graph_tables_rebuilds_symbols_and_references_from_ids_and_edges() -> None:
    verify = "shared/auth.py::verify_token"
    charge = "billing/invoice.py::charge"
    odd = "a::b/c.py::Cls.m"  # a path may hold "::"; a qualname never does
    call = f"billing/invoice.py:{charge}->{verify}:call"
    imp = f"billing/invoice.py:->{verify}:import"  # module level: no from_symbol edge
    dup = "x/y.py::dup"
    nodes, edges = _graph(
        ("s1", "symbols", _text("symbols", f"  id: {verify}", "  line: 999")),
        ("s2", "symbols", _text("symbols", f"  id: {charge}")),
        ("s3", "symbols", _text("symbols", f"  id: {odd}")),
        ("d1", "symbols", _text("symbols", f"  id: {dup}")),
        ("d2", "symbols", _text("symbols", f"  id: {dup}")),  # ambiguous: refused
        ("r1", "references", _text("references", f"  id: {call}")),
        ("r2", "references", _text("references", f"  id: {imp}")),
        ("r3", "references", _text("references", f"  id: x/z.py:->{dup}:import")),
        ("r4", "references", _text("references", "  id: no/edges.py:->x:call")),
    )
    edges += [
        ("r1", "s2", FROM_SYMBOL, {}),
        ("r1", "s1", TO_SYMBOL, {}),
        ("r2", "s1", TO_SYMBOL, {}),
        ("r3", "d1", TO_SYMBOL, {}),
        ("r3", "d2", TO_SYMBOL, {}),
    ]

    invoice = "billing/invoice.py"
    assert graph_tables(nodes, edges) == {
        "symbols": [
            {"id": odd, "file": "a::b/c.py"},
            {"id": charge, "file": invoice},
            {"id": verify, "file": "shared/auth.py"},
        ],
        "references": [
            {"id": imp, "from_symbol": None, "to_symbol": verify, "from_file": invoice},
            {"id": call, "from_symbol": charge, "to_symbol": verify, "from_file": invoice},
        ],
    }


def test_graph_tables_reference_with_a_missing_owner_edge_has_no_file() -> None:
    # an owned call whose from_symbol edge is gone must not pass for a module-level import
    verify = "shared/auth.py::verify_token"
    call = f"billing/invoice.py:billing/invoice.py::charge->{verify}:call"
    nodes, edges = _graph(
        ("s1", "symbols", _text("symbols", f"  id: {verify}")),
        ("r1", "references", _text("references", f"  id: {call}")),
    )
    edges.append(("r1", "s1", TO_SYMBOL, {}))

    [ref] = graph_tables(nodes, edges)["references"]
    assert ref["from_symbol"] is None
    assert ref["from_file"] is None


def test_graph_tables_blast_equals_the_row_blast(tables: Tables) -> None:
    # The connector-only PR has no blast module; this check belongs to the blast-radius PR.
    pytest.importorskip("cognee_community_connector_gitgraph.blast")
    from cognee_community_connector_gitgraph.blast import blast_radius

    graph = graph_tables(*_linked(tables))
    assert len(graph["symbols"]) == len(tables["symbols"])
    assert len(graph["references"]) == len(tables["references"])
    for changed in (["shared/auth.py"], ["shared/auth.py:3"]):
        rows = blast_radius(tables, changed)
        walked = blast_radius(tables | graph, changed)
        for key in ("symbols", "files", "services"):
            assert walked[key] == rows[key], key
