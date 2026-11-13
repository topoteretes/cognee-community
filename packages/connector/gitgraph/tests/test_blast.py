"""Blast radius: which symbols, files and services a change can break, from the rows alone."""

import json
import random
from pathlib import Path
from typing import Any

import pytest

from cognee_community_connector_gitgraph import build_sample_repo
from cognee_community_connector_gitgraph.blast import blast_radius
from cognee_community_connector_gitgraph.ci_logs import parse_ci_logs
from cognee_community_connector_gitgraph.git_history import extract_commits
from cognee_community_connector_gitgraph.symbols import extract_symbols

FIXTURES = Path(__file__).parent / "fixtures" / "ci"
AUTH = "shared/auth.py"
CHAIN = [
    "shared/auth.py::verify_token",
    "shared/session.py::load_session",
    "orders/cart.py::get_cart",
    "orders/checkout.py::checkout",
    "notifications/receipts.py::send_receipt",
]

Tables = dict[str, list[dict[str, Any]]]


@pytest.fixture
def tables(tmp_path: Path) -> Tables:
    repo = build_sample_repo(tmp_path / "repo")
    history = extract_commits(repo.path, [repo.base, repo.head])
    code = extract_symbols(repo.path, repo.head)
    return {
        "commits": history["commits"],
        "file_changes": history["file_changes"],
        "symbols": code["symbols"],
        "references": code["references"],
        "ci_failures": parse_ci_logs([FIXTURES])["ci_failures"],
    }


def _by_name(items: list[dict[str, Any]], key: str = "name") -> dict[str, dict[str, Any]]:
    return {item[key]: item for item in items}


def _line(tables: Tables, symbol_id: str) -> int:
    return next(s["line"] for s in tables["symbols"] if s["id"] == symbol_id)


def test_sample_blast_of_auth(tables: Tables) -> None:
    out = blast_radius(tables, [AUTH], max_hops=4)
    assert out["changed_files"] == [AUTH]
    assert out["changed_symbols"] == sorted([CHAIN[0], "shared/auth.py::make_token"])
    services = _by_name(out["services"])
    assert list(services) == ["billing", "notifications", "orders"]
    assert services["notifications"]["hops"] == 4
    assert services["notifications"]["path"] == CHAIN
    assert services["billing"]["hops"] == 1
    assert services["billing"]["path"] == [CHAIN[0], "billing/invoice.py::charge"]
    symbols = _by_name(out["symbols"], "id")
    assert symbols[CHAIN[4]]["hops"] == 4
    assert symbols[CHAIN[4]]["path"] == CHAIN
    assert symbols[CHAIN[4]]["file"] == "notifications/receipts.py"
    assert AUTH not in _by_name(out["files"], "file")
    assert "search/index.py" not in _by_name(out["files"], "file")
    json.dumps(out)


def test_sample_ci_failures_and_co_changed(tables: Tables) -> None:
    out = blast_radius(tables, [AUTH])
    failures = _by_name(out["ci_failures"], "job")
    assert sorted(failures) == ["billing-tests", "orders-tests"]
    assert failures["billing-tests"]["service"] == "billing"
    assert failures["orders-tests"]["service"] == "orders"
    assert {f["id"] for f in out["ci_failures"]} == {
        f["id"] for f in tables["ci_failures"] if f["job"] != "search-tests"
    }
    expiry = next(c["sha"] for c in tables["commits"] if c["message"] == "add token expiry")
    co = _by_name(out["co_changed"], "path")
    assert co["billing/invoice.py"]["shas"] == [expiry]
    assert AUTH not in co
    # co_changed is informational: shared/__init__.py changed with auth but adds no service
    assert "shared" not in _by_name(out["services"])


def test_co_changed_sorted_by_shared_commits(tables: Tables) -> None:
    out = blast_radius(tables, [AUTH])
    keys = [(-len(c["shas"]), c["path"]) for c in out["co_changed"]]
    assert keys == sorted(keys)
    for c in out["co_changed"]:
        assert c["shas"] == sorted(c["shas"])


def test_max_hops_limits_the_walk(tables: Tables) -> None:
    three = blast_radius(tables, [AUTH], max_hops=3)
    assert list(_by_name(three["services"])) == ["billing", "orders"]
    assert all(s["hops"] <= 3 for s in three["symbols"])
    zero = blast_radius(tables, [AUTH], max_hops=0)
    assert zero["symbols"] == []
    assert zero["files"] == []
    assert zero["services"] == []
    assert zero["max_hops"] == 0


def test_line_inside_verify_token_matches_whole_file(tables: Tables) -> None:
    line = _line(tables, CHAIN[0]) + 1
    by_line = blast_radius(tables, [f"{AUTH}:{line}"])
    assert by_line["changed_symbols"] == [CHAIN[0]]
    whole = blast_radius(tables, [AUTH])
    assert by_line["services"] == whole["services"]
    assert by_line["changed_files"] == [AUTH]


def test_line_inside_make_token_reaches_nothing(tables: Tables) -> None:
    # nothing in the sample calls make_token
    line = _line(tables, "shared/auth.py::make_token") + 1
    out = blast_radius(tables, [f"{AUTH}:{line}"])
    assert out["changed_symbols"] == ["shared/auth.py::make_token"]
    assert out["services"] == []
    assert out["symbols"] == []


def test_module_level_line_changes_every_symbol_in_the_file(tables: Tables) -> None:
    out = blast_radius(tables, [f"{AUTH}:1"])
    assert out["changed_symbols"] == blast_radius(tables, [AUTH])["changed_symbols"]


def test_unknown_path_has_no_symbols_but_is_listed(tables: Tables) -> None:
    out = blast_radius(tables, ["nope/missing.py"])
    assert out["changed_files"] == ["nope/missing.py"]
    assert out["symbols"] == []
    assert out["services"] == []
    assert out["co_changed"] == []


def test_isolated_service_has_no_blast(tables: Tables) -> None:
    out = blast_radius(tables, ["search/index.py"])
    assert out["services"] == []
    assert out["ci_failures"] == [
        {
            "id": f["id"],
            "job": f["job"],
            "test_id": f["test_id"],
            "service": "search",
            "message": f["message"],
        }
        for f in tables["ci_failures"]
        if f["job"] == "search-tests"
    ]


def test_tables_may_be_missing(tables: Tables) -> None:
    out = blast_radius({"symbols": tables["symbols"], "references": tables["references"]}, [AUTH])
    assert list(_by_name(out["services"])) == ["billing", "notifications", "orders"]
    assert out["co_changed"] == []
    assert out["ci_failures"] == []


def test_root_level_files_belong_to_service_dot() -> None:
    tables: Tables = {
        "symbols": [
            {"id": "a.py::f", "kind": "function", "name": "f", "file": "a.py", "line": 1},
            {"id": "b/c.py::g", "kind": "function", "name": "g", "file": "b/c.py", "line": 1},
        ],
        "references": [
            {
                "id": "r1",
                "from_file": "a.py",
                "from_symbol": "a.py::f",
                "to_symbol": "b/c.py::g",
                "kind": "call",
                "line": 2,
            }
        ],
    }
    out = blast_radius(tables, ["b/c.py"])
    assert [s["name"] for s in out["services"]] == ["."]
    assert out["ci_failures"] == []


def test_module_level_import_marks_the_importing_file(tables: Tables) -> None:
    out = blast_radius(tables, ["shared/session.py"])
    files = _by_name(out["files"], "file")
    assert files["orders/cart.py"]["hops"] == 1
    assert files["orders/cart.py"]["path"] == [
        "shared/session.py::load_session",
        "orders/cart.py::get_cart",
    ]


def test_output_is_deterministic_and_order_independent(tables: Tables) -> None:
    first = blast_radius(tables, [AUTH])
    assert blast_radius(tables, [AUTH]) == first
    rng = random.Random(7)
    for _ in range(20):
        shuffled = {}
        for name, rows in tables.items():
            rows = list(rows)
            rng.shuffle(rows)
            shuffled[name] = rows
        assert blast_radius(shuffled, [AUTH]) == first


@pytest.mark.parametrize("bad", [-1, 1.5, "2", None, True])
def test_bad_max_hops_raises(tables: Tables, bad: Any) -> None:
    with pytest.raises(ValueError):
        blast_radius(tables, [AUTH], max_hops=bad)


def test_empty_changed_gives_empty_results(tables: Tables) -> None:
    out = blast_radius(tables, [])
    for key in ("changed_files", "changed_symbols", "symbols", "files", "services"):
        assert out[key] == []
    assert out["co_changed"] == []
    assert out["ci_failures"] == []


def _random_tables(rng: random.Random) -> Tables:
    files = [f"d{i}/m{j}.py" for i in range(3) for j in range(2)] + ["root.py"]
    symbols = [
        {"id": f"{f}::s{k}", "kind": "function", "name": f"s{k}", "file": f, "line": 1 + 3 * k}
        for f in files
        for k in range(rng.randint(0, 2))
    ]
    ids: list[str] = [str(s["id"]) for s in symbols]
    references = []
    for n in range(rng.randint(0, 14)):
        if not ids:
            break
        target = rng.choice(ids)
        owner: str | None = rng.choice([*ids, None])
        file = owner.split("::")[0] if owner else rng.choice(files)
        references.append(
            {
                "id": f"r{n}",
                "from_file": file,
                "from_symbol": owner,
                "to_symbol": target,
                "kind": "call" if owner else "import",
                "line": 1,
            }
        )
    return {"symbols": symbols, "references": references}


def test_random_graphs_hold_the_invariants() -> None:
    for seed in range(300):
        rng = random.Random(seed)
        tables = _random_tables(rng)
        known = sorted({s["file"] for s in tables["symbols"]} | {"root.py"})
        changed = rng.sample(known, min(len(known), rng.randint(1, 2)))
        max_hops = rng.randint(0, 5)
        out = blast_radius(tables, changed, max_hops=max_hops)
        edges = {(r["from_symbol"] or r["from_file"], r["to_symbol"]) for r in tables["references"]}
        changed_syms = set(out["changed_symbols"])
        changed_files = set(out["changed_files"])
        for hit in [*out["symbols"], *out["files"], *out["services"]]:
            assert hit["hops"] <= max_hops
            assert hit["hops"] == len(hit["path"]) - 1
            assert hit["path"][0] in changed_syms
            for callee, caller in zip(hit["path"], hit["path"][1:], strict=False):
                assert (caller, callee) in edges
        assert not changed_syms & {s["id"] for s in out["symbols"]}
        assert not changed_files & {f["file"] for f in out["files"]}
        top = {f.split("/")[0] if "/" in f else "." for f in changed_files}
        assert not top & {s["name"] for s in out["services"]}
        assert [s["id"] for s in out["symbols"]] == sorted(s["id"] for s in out["symbols"])
        # shuffling the input never changes the answer
        shuffled = {k: rng.sample(v, len(v)) for k, v in tables.items()}
        assert blast_radius(shuffled, changed, max_hops=max_hops) == out


def test_disconnected_component_never_appears() -> None:
    for seed in range(300):
        rng = random.Random(seed)
        tables = _random_tables(rng)
        island = {
            "id": "island/x.py::z",
            "kind": "function",
            "name": "z",
            "file": "island/x.py",
            "line": 1,
        }
        tables["symbols"].append(island)
        tables["references"].append(
            {
                "id": "ri",
                "from_file": "island/x.py",
                "from_symbol": "island/x.py::z",
                "to_symbol": "island/x.py::z",
                "kind": "call",
                "line": 1,
            }
        )
        out = blast_radius(tables, ["d0/m0.py"])
        assert "island" not in {s["name"] for s in out["services"]}
        assert "island/x.py::z" not in {s["id"] for s in out["symbols"]}


def _sym(file: str, name: str, line: int, end: int) -> dict[str, Any]:
    sid = f"{file}::{name}"
    return {"id": sid, "name": name, "file": file, "line": line, "end_line": end}


def _ranged() -> Tables:
    return {
        "symbols": [
            _sym("m.py", "A", 5, 20),
            _sym("m.py", "A.one", 6, 10),
            _sym("m.py", "A.two", 12, 20),
            _sym("m.py", "top", 25, 30),
        ]
    }


def test_range_overlaps_one_function_only() -> None:
    out = blast_radius(_ranged(), ["m.py:26-27"])
    assert out["changed_symbols"] == ["m.py::top"]
    assert out["changed_files"] == ["m.py"]


def test_range_touching_a_function_edge_counts() -> None:
    assert blast_radius(_ranged(), ["m.py:30-31"])["changed_symbols"] == ["m.py::top"]
    assert blast_radius(_ranged(), ["m.py:23-25"])["changed_symbols"] == ["m.py::top"]


def test_range_in_a_method_marks_the_method_not_its_class() -> None:
    out = blast_radius(_ranged(), ["m.py:7-8"])
    assert out["changed_symbols"] == ["m.py::A.one"]


def test_range_in_class_body_outside_methods_marks_the_class() -> None:
    assert blast_radius(_ranged(), ["m.py:11-11"])["changed_symbols"] == ["m.py::A"]


def test_range_over_two_methods_marks_both_not_the_class() -> None:
    out = blast_radius(_ranged(), ["m.py:9-13"])
    assert out["changed_symbols"] == ["m.py::A.one", "m.py::A.two"]


def test_module_level_range_marks_no_symbol_but_keeps_the_file() -> None:
    out = blast_radius(_ranged(), ["m.py:1-3"])
    assert out["changed_symbols"] == []
    assert out["changed_files"] == ["m.py"]


def test_range_on_rows_without_lines_marks_every_symbol_in_the_file() -> None:
    rows = {"symbols": [{"id": "m.py::a", "file": "m.py"}, {"id": "m.py::b", "file": "m.py"}]}
    assert blast_radius(rows, ["m.py:1-2"])["changed_symbols"] == ["m.py::a", "m.py::b"]


def test_explicit_changed_symbols_override_the_items() -> None:
    rows = {"symbols": [{"id": "m.py::a", "file": "m.py"}, {"id": "m.py::b", "file": "m.py"}]}
    out = blast_radius(rows, ["m.py"], changed_symbols=["m.py::b"])
    assert out["changed_symbols"] == ["m.py::b"]
    assert out["changed_files"] == ["m.py"]


def _commit_rows(sha: str, paths: list[str]) -> list[dict[str, Any]]:
    return [{"sha": sha, "path": p} for p in paths]


def test_bulk_commits_are_ignored_for_co_changed() -> None:
    rows = _ranged() | {
        "file_changes": _commit_rows("small", ["m.py", "near.py"])
        + _commit_rows("bulk", ["m.py"] + [f"f{i}.py" for i in range(20)])
        + _commit_rows("edge", ["m.py", "edge.py"] + [f"g{i}.py" for i in range(18)])
    }
    out = blast_radius(rows, ["m.py"])
    paths = {c["path"] for c in out["co_changed"]}
    assert "near.py" in paths and "edge.py" in paths  # 20 files is not bulk
    assert not any(p.startswith("f") for p in paths)  # 21 files is
