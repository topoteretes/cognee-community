"""The 3D blast page: graph data, statuses, safe embedding, the vendored library, the CLI flag."""

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from test_cli import _args, _keyless

from cognee_community_connector_gitgraph import build_sample_repo
from cognee_community_connector_gitgraph.blast import blast_radius
from cognee_community_connector_gitgraph.cli import main
from cognee_community_connector_gitgraph.git_history import extract_commits
from cognee_community_connector_gitgraph.html import VENDOR, graph_data, render_html
from cognee_community_connector_gitgraph.symbols import extract_symbols

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
    }


@pytest.fixture
def result(tables: Tables) -> dict[str, Any]:
    out = blast_radius(tables, ["shared/auth.py"], max_hops=4)
    return out | {"base": "main", "head": "feature/rotate-tokens"}


def _embedded(page: str) -> dict[str, Any]:
    match = re.search(r'<script id="graph-data" type="application/json">(.*?)</script>', page, re.S)
    assert match
    data: dict[str, Any] = json.loads(match.group(1))
    return data


def test_vendored_library_loads_from_the_package() -> None:
    lib = VENDOR / "3d-force-graph.min.js"
    assert lib.is_file()
    assert "ForceGraph3D" in lib.read_text()
    assert (VENDOR / "LICENSE.txt").read_text().startswith("3d-force-graph.min.js")


def test_wheel_config_does_not_exclude_the_vendor_dir() -> None:
    config = (Path(__file__).parent.parent / "pyproject.toml").read_text()
    assert "exclude" not in config
    assert "vendor" not in config.split("[tool.hatch.build.targets.wheel]")[1].split("[tool.")[0]
    assert Path(VENDOR).parent.name == "cognee_community_connector_gitgraph"


def test_statuses_for_the_sample(tables: Tables, result: dict[str, Any]) -> None:
    data = graph_data(tables, result)
    nodes = {n["id"]: n for n in data["nodes"]}

    assert len(nodes) == 8
    assert nodes["shared/auth.py::verify_token"]["status"] == "changed"
    assert nodes["shared/auth.py::make_token"]["status"] == "changed"
    assert nodes["billing/invoice.py::charge"]["status"] == "at-risk"
    assert nodes["billing/invoice.py::charge"]["hops"] == 1
    assert nodes["notifications/receipts.py::send_receipt"]["hops"] == 4
    assert nodes["search/index.py::search"]["status"] == "safe"
    assert nodes["billing/invoice.py::charge"]["service"] == "billing"
    assert nodes["billing/invoice.py::charge"]["label"] == "billing/invoice.py::charge (1 hop)"
    assert nodes["orders/cart.py::get_cart"]["label"].endswith("(2 hops)")
    assert nodes["search/index.py::search"]["label"] == "search/index.py::search"
    links = {(link["source"], link["target"]) for link in data["links"]}
    assert ("billing/invoice.py::charge", "shared/auth.py::verify_token") in links
    assert ("orders/cart.py::get_cart", "shared/session.py::load_session") in links
    assert all(s in nodes and t in nodes for s, t in links)


def test_page_inlines_the_library_and_loads_nothing(tables: Tables, result: dict[str, Any]) -> None:
    page = render_html(tables, result)

    assert "ForceGraph3D" in page
    assert "window.graph" in page
    assert 'src="http' not in page
    assert page.count("<script") == 3  # the library, the data and the page script
    assert "main...feature/rotate-tokens" in page
    panel = page.split('id="panel"', 1)[1]
    for name, hops in (("billing", "1 hop"), ("orders", "2 hops"), ("notifications", "4 hops")):
        assert re.search(rf"{name}[^<]*{hops}", panel)
    assert "search" not in panel.split("</aside>")[0]
    assert _embedded(page)["nodes"]


def test_ids_cannot_break_out_of_the_script_or_the_markup(
    tables: Tables, result: dict[str, Any]
) -> None:
    evil = "x/y.py::</script><script>alert(1)</script>"
    tables["symbols"].append(
        {"id": evil, "kind": "function", "name": "f", "file": "x/y.py", "line": 1}
    )
    result["services"].append({"name": "<b>svc</b>", "hops": 2, "path": []})
    result["head"] = "<img src=x>"

    page = render_html(tables, result)

    assert "<script>alert(1)" not in page
    assert "<img src=x>" not in page
    assert "<b>svc</b>" not in page
    assert "&lt;b&gt;svc&lt;/b&gt;" in page
    assert evil in {n["id"] for n in _embedded(page)["nodes"]}


def _stdout(argv: list[str]) -> subprocess.CompletedProcess[str]:
    code = "import sys; from cognee_community_connector_gitgraph.cli import main; sys.exit(main())"
    return subprocess.run(
        [sys.executable, "-c", code, *argv],
        capture_output=True,
        text=True,
        check=False,
        env=_keyless(),
    )


def test_blast_html_writes_the_page_and_leaves_stdout_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = build_sample_repo(tmp_path / "repo").path
    assert main(_args(repo)) == 0
    plain = capsys.readouterr()

    out = tmp_path / "out.html"
    assert main(_args(repo, "--html", str(out))) == 0
    with_html = capsys.readouterr()

    assert with_html.out == plain.out
    assert with_html.err.strip() == f"wrote {out}"
    assert "ForceGraph3D" in out.read_text()
    assert main(_args(repo, "--json", "--html", str(out))) == 0
    assert json.loads(capsys.readouterr().out)["head"] == "feature/rotate-tokens"


@pytest.mark.integration
def test_demo_html_writes_the_page_and_leaves_stdout_alone(tmp_path: Path) -> None:
    out = tmp_path / "demo.html"
    plain = _stdout(["demo"])
    with_html = _stdout(["demo", "--html", str(out)])

    assert with_html.returncode == 0, with_html.stderr
    assert with_html.stdout == plain.stdout
    assert with_html.stderr.strip() == f"wrote {out}"
    assert len(_embedded(out.read_text())["nodes"]) == 8
