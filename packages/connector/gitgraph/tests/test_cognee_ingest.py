"""The source ingested by real cognee (local embedded stores, no LLM key)."""

import re
from collections import Counter
from itertools import pairwise
from pathlib import Path
from typing import Any

import cognee
import pytest
from cognee.infrastructure.databases.graph import get_graph_engine
from git import Repo

from cognee_community_connector_gitgraph import (
    build_sample_repo,
    gitgraph_source,
    link_rows,
)
from cognee_community_connector_gitgraph.links import graph_tables

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parent / "fixtures" / "ci"
COUNTS = {"commits": 8, "file_changes": 15, "symbols": 8, "references": 10, "ci_failures": 3}
LINKS = (
    "sha_references_commits",
    "from_symbol_references_symbols",
    "to_symbol_references_symbols",
    "commit_sha_references_commits",
)
# 15 file changes, 5 calls made inside a symbol, 10 references, 1 CI failure on a real commit.
LINK_COUNTS = {LINKS[0]: 15, LINKS[1]: 5, LINKS[2]: 10, LINKS[3]: 1}


@pytest.fixture(autouse=True)
def keyless(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("LLM_API_KEY", "OPENAI_API_KEY", "EMBEDDING_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def _source(repo: Path, tmp_path: Path) -> Any:
    """The sample source plus one CI failure on the head commit by short sha (the fixtures name
    no real commit). Every test uses this same data: they share one cognee dataset (see conftest)
    and cognee does not pick up a row added to a source the dataset already holds.
    """
    sha = Repo(repo).head.commit.hexsha[:7]
    extra = tmp_path / "extra.xml"
    extra.write_text(
        '<testsuites><testsuite name="s"><properties>'
        f'<property name="GITHUB_SHA" value="{sha}"/></properties>'
        '<testcase classname="x.test_y" name="test_z"><failure message="boom">b</failure>'
        "</testcase></testsuite></testsuites>"
    )
    return gitgraph_source(repo, ci_logs=[FIXTURES, extra])


async def _remember(repo: Path, tmp_path: Path) -> Any:
    source = _source(repo, tmp_path)
    result = await cognee.remember(source, dataset_name="gitgraph", self_improvement=False)
    assert result.status == "completed", result.error
    return source


async def _graph() -> tuple[dict[str, tuple[str, dict[str, Any]]], list[tuple[str, str, str]]]:
    """Row nodes by node id as (table, properties), and every edge as (source, target, name).

    Cognee links each row to its SchemaTable node with ``is_row_of``.
    """
    nodes, edges = await (await get_graph_engine()).get_graph_data()
    by_id = {str(node_id): props for node_id, props in nodes}
    is_row_of = [(str(s), str(t)) for s, t, name, _ in edges if name == "is_row_of"]
    rows = {s: (by_id[t]["name"], by_id[s]) for s, t in is_row_of}
    return rows, [(str(s), str(t), name) for s, t, name, _ in edges]


async def _row_nodes() -> dict[str, list[dict[str, Any]]]:
    rows, _ = await _graph()
    by_table: dict[str, list[dict[str, Any]]] = {}
    for table, props in rows.values():
        by_table.setdefault(table, []).append(props)
    return by_table


async def _link_counts() -> Counter[str]:
    _, edges = await _graph()
    return Counter(name for _, _, name in edges if name in LINKS)


def _row_id(text: str) -> str:
    found = re.search(r"^  id: (.*)$", text, re.MULTILINE)
    assert found
    return found.group(1)


async def test_remember_puts_every_row_in_the_graph(tmp_path: Path) -> None:
    await _remember(build_sample_repo(tmp_path / "repo").path, tmp_path)

    rows = await _row_nodes()

    assert {table: len(r) for table, r in rows.items()} == COUNTS
    assert any("shared/auth.py::verify_token" in r["text"] for r in rows["symbols"])


async def test_id_is_the_first_row_data_line_of_every_row(tmp_path: Path) -> None:
    await _remember(build_sample_repo(tmp_path / "repo").path, tmp_path)

    for table, rows in (await _row_nodes()).items():
        assert rows
        for props in rows:
            _, _, data = props["text"].partition("\nRow Data:\n")
            assert data.startswith("  id: "), (table, props["text"])


async def test_remembering_twice_adds_no_row_nodes(tmp_path: Path) -> None:
    repo = build_sample_repo(tmp_path / "repo").path
    await _remember(repo, tmp_path)
    await _remember(repo, tmp_path)

    rows = await _row_nodes()

    assert Counter({table: len(r) for table, r in rows.items()}) == Counter(COUNTS)


async def test_link_rows_adds_exact_edges_per_relationship(tmp_path: Path) -> None:
    repo = build_sample_repo(tmp_path / "repo").path

    summary = await link_rows(await _remember(repo, tmp_path))

    assert await _link_counts() == LINK_COUNTS
    assert summary == {**LINK_COUNTS, "missing": 0, "ambiguous": 0}


async def test_link_rows_twice_and_remember_twice_adds_no_edges(tmp_path: Path) -> None:
    repo = build_sample_repo(tmp_path / "repo").path
    await link_rows(await _remember(repo, tmp_path))
    nodes_before, edges_before = await _graph()

    again = await link_rows(await _remember(repo, tmp_path))
    await link_rows(_source(repo, tmp_path))
    nodes_after, edges_after = await _graph()

    assert await _link_counts() == LINK_COUNTS
    assert again == {**LINK_COUNTS, "missing": 0, "ambiguous": 0}
    assert len(nodes_after) == len(nodes_before)
    assert Counter(edges_after) == Counter(edges_before)


async def test_call_chain_is_walkable_through_the_new_edges(tmp_path: Path) -> None:
    repo = build_sample_repo(tmp_path / "repo").path
    await link_rows(await _remember(repo, tmp_path))
    rows, edges = await _graph()
    symbol_of = {n: _row_id(p["text"]) for n, (table, p) in rows.items() if table == "symbols"}
    reference_to = {s: symbol_of[t] for s, t, name in edges if name == LINKS[2]}
    callers_of: dict[str, set[str]] = {}
    for source, target, name in edges:
        if name == LINKS[1]:
            callers_of.setdefault(reference_to[source], set()).add(symbol_of[target])

    chain = [
        "shared/auth.py::verify_token",
        "shared/session.py::load_session",
        "orders/cart.py::get_cart",
        "orders/checkout.py::checkout",
        "notifications/receipts.py::send_receipt",
    ]
    for callee, caller in pairwise(chain):
        assert caller in callers_of[callee], (callee, caller)
    # charge also calls verify_token directly; nothing calls into or out of search.
    assert callers_of["shared/auth.py::verify_token"] == {
        "shared/session.py::load_session",
        "billing/invoice.py::charge",
    }
    assert "search/index.py::search" not in set().union(*callers_of.values())
    assert "search/index.py::search" not in callers_of


async def test_blast_over_the_cognee_graph_equals_the_row_blast(tmp_path: Path) -> None:
    # The connector-only PR has no blast module; this check belongs to the blast-radius PR.
    pytest.importorskip("cognee_community_connector_gitgraph.blast")
    from cognee_community_connector_gitgraph.blast import blast_radius

    repo = build_sample_repo(tmp_path / "repo").path
    source = await _remember(repo, tmp_path)
    await link_rows(source)
    graph = graph_tables(*await (await get_graph_engine()).get_graph_data())
    tables = {name: list(resource) for name, resource in source.resources.items()}

    walked = blast_radius(graph, ["shared/auth.py"])
    rows = blast_radius(tables, ["shared/auth.py"])

    for key in ("changed_symbols", "symbols", "files", "services"):
        assert walked[key] == rows[key], key
    assert {s["name"]: s["hops"] for s in walked["services"]} == {
        "billing": 1,
        "orders": 2,
        "notifications": 4,
    }
