"""The ``cognee-gitgraph`` command line: blast report, JSON, --remember, demo and error exits."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from git import Repo

from cognee_community_connector_gitgraph import build_sample_repo, cli
from cognee_community_connector_gitgraph.cli import main

CI = Path(__file__).parent / "fixtures" / "ci"
BLAST_KEYS = {
    "base",
    "head",
    "changed_files",
    "changed_symbols",
    "max_hops",
    "symbols",
    "files",
    "services",
    "co_changed",
    "ci_failures",
    "computed_from",
}
FROM_GRAPH = "Callers found by walking Cognee's graph"
FROM_ROWS = "Callers found by walking the extracted rows"
SEND_RECEIPT_PATH = (
    "shared/auth.py::verify_token -> shared/session.py::load_session -> "
    "orders/cart.py::get_cart -> orders/checkout.py::checkout -> "
    "notifications/receipts.py::send_receipt"
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return build_sample_repo(tmp_path / "repo").path


def _args(repo: Path, *extra: str) -> list[str]:
    head = "feature/rotate-tokens"
    return ["blast", "--repo", str(repo), "--base", "main", "--head", head, *extra]


def _run(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, str, str]:
    code = main(argv)
    out, err = capsys.readouterr()
    return code, out, err


def _json(capsys: pytest.CaptureFixture[str], repo: Path, *extra: str) -> dict[str, Any]:
    code, out, err = _run(capsys, _args(repo, "--json", *extra))
    assert code == 0, err
    result: dict[str, Any] = json.loads(out)
    return result


def test_text_report_lists_services_and_the_four_hop_path(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _run(capsys, _args(repo))

    assert code == 0
    assert err == ""
    assert "shared/auth.py" in out
    assert "billing" in out
    assert "orders" in out
    assert "notifications" in out
    assert "notifications  4 hops  " + SEND_RECEIPT_PATH in out
    assert "billing  1 hop  " in out
    assert "search" not in out
    assert FROM_ROWS in out


def test_json_has_exactly_the_blast_keys_and_services(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = _json(capsys, repo)

    assert set(result) == BLAST_KEYS
    assert result["computed_from"] == "rows"
    assert result["base"] == "main"
    assert result["head"] == "feature/rotate-tokens"
    assert result["max_hops"] == 4
    assert result["changed_files"] == ["shared/auth.py"]
    hops = {s["name"]: s["hops"] for s in result["services"]}
    assert hops == {"billing": 1, "notifications": 4, "orders": 2}


def test_hops_option_drops_notifications(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    result = _json(capsys, repo, "--hops", "3")

    assert result["max_hops"] == 3
    assert [s["name"] for s in result["services"]] == ["billing", "orders"]


def test_ci_failures_are_listed_for_at_risk_services(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(capsys, _args(repo, "--ci", str(CI)))
    assert code == 0
    assert "billing-tests" in out
    assert "orders-tests" in out
    assert "search" not in out

    result = _json(capsys, repo, "--ci", str(CI))
    assert sorted(f["job"] for f in result["ci_failures"]) == ["billing-tests", "orders-tests"]


def test_stdout_is_only_json_in_json_mode(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _, out, _ = _run(capsys, _args(repo, "--json"))
    assert out.startswith("{")
    assert out.endswith("}\n")
    json.loads(out)


def _assert_error(
    capsys: pytest.CaptureFixture[str], argv: list[str], needle: str | None = None
) -> None:
    code, out, err = _run(capsys, argv)
    assert code == 2
    assert out == ""
    assert err.startswith("error: ")
    assert err.count("\n") == 1
    assert "Traceback" not in err
    if needle:
        assert needle in err


def test_errors_exit_2_with_one_line(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    _assert_error(capsys, _args(plain), "not a git repository")
    _assert_error(
        capsys,
        ["blast", "--repo", str(repo), "--base", "main", "--head", "nope"],
        "unknown ref",
    )
    _assert_error(capsys, _args(repo, "--ci", str(tmp_path / "missing.xml")), "no such path")
    _assert_error(capsys, _args(repo, "--hops", "-1"), "hops")


def test_usage_errors_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["blast"])
    assert exc.value.code == 2
    assert "usage" in capsys.readouterr().err


def test_quiet_run_drops_fastembed_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    # fastembed logs through loguru, which LOG_LEVEL does not reach: fastembed 0.9.1 logs a
    # cache notice at INFO on each model load when an older fastembed filled the HF cache.
    from loguru import logger

    names: list[str] = []
    sink = logger.add(lambda message: names.append(message.record["name"] or ""))

    def run(args: Any) -> dict[str, Any]:
        exec("from loguru import logger\nlogger.info('cache notice')", {"__name__": "fastembed.x"})
        raise ValueError("stop")

    monkeypatch.setattr(cli, "_blast", run)
    monkeypatch.setenv("LOG_LEVEL", "ERROR")  # so main() leaves the env as it found it
    try:
        assert main(_args(Path("."))) == 2
    finally:
        logger.remove(sink)
        logger.enable("fastembed")
    assert "fastembed.x" not in names


def _script() -> str:
    return str(Path(sys.executable).parent / "cognee-gitgraph")


def test_error_is_the_only_stderr_line(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    done = subprocess.run(
        [_script(), *_args(plain)], capture_output=True, text=True, check=False, env=_keyless()
    )

    assert done.returncode == 2
    assert done.stdout == ""
    assert done.stderr.startswith("error: ")
    assert done.stderr.count("\n") == 1


def test_clean_blast_has_empty_stderr(repo: Path) -> None:
    done = subprocess.run(
        [_script(), *_args(repo)], capture_output=True, text=True, check=False, env=_keyless()
    )

    assert done.returncode == 0
    assert done.stderr == ""
    assert "Blast radius of main...feature/rotate-tokens" in done.stdout


def test_one_shared_commit_is_singular() -> None:
    from cognee_community_connector_gitgraph.cli import _report

    result = {
        "base": "a",
        "head": "b",
        "max_hops": 1,
        "changed_files": [],
        "changed_symbols": [],
        "services": [],
        "symbols": [],
        "files": [],
        "co_changed": [{"path": "x.py", "shas": ["1"]}, {"path": "y.py", "shas": ["1", "2"]}],
        "ci_failures": [],
        "computed_from": "rows",
    }
    report = _report(result)

    assert "x.py  (1 shared commit)" in report
    assert "y.py  (2 shared commits)" in report


def test_console_script_runs(repo: Path) -> None:
    script = Path(sys.executable).parent / "cognee-gitgraph"
    done = subprocess.run(
        [str(script), *_args(repo, "--json")], capture_output=True, text=True, check=False
    )

    assert done.returncode == 0, done.stderr
    assert set(json.loads(done.stdout)) == BLAST_KEYS

    bad = subprocess.run(
        [str(script), *_args(repo, "--hops", "-1")], capture_output=True, text=True, check=False
    )
    assert bad.returncode == 2
    assert bad.stderr.startswith("error: ")
    assert "Traceback" not in bad.stderr


# --remember: real cognee, no key. Cognee caches its stores per process, so each test runs the CLI
# in a fresh process with its own cognee home (the shared test graph stays untouched).

_PRELUDE = """
import sys
import cognee
home = sys.argv[1]
cognee.config.system_root_directory(home + "/system")
cognee.config.data_root_directory(home + "/data")
"""
_CLI = (
    _PRELUDE
    + """
from cognee_community_connector_gitgraph.cli import main
sys.exit(main(sys.argv[2:]))
"""
)
# Like _CLI, then lists the text of every commit row in the graph of the dataset it remembered
# (cognee keeps one graph per dataset).
_CLI_COMMITS = (
    _PRELUDE
    + """
import asyncio, contextlib, io, json
from cognee.context_global_variables import set_database_global_context_variables
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.modules.users.methods import get_default_user
from cognee_community_connector_gitgraph.cli import main

async def commit_rows(name):
    user = await get_default_user()
    dataset = next(d for d in await cognee.datasets.list_datasets(user) if d.name == name)
    async with set_database_global_context_variables(dataset.id, user.id):
        nodes, edges = await (await get_graph_engine()).get_graph_data()
    tables = {str(node_id): props["name"] for node_id, props in nodes}
    rows = {str(s) for s, t, n, _ in edges if n == "is_row_of" and tables[str(t)] == "commits"}
    return [props["text"] for node_id, props in nodes if str(node_id) in rows]

out = io.StringIO()
with contextlib.redirect_stdout(out):
    code = main(sys.argv[2:])
result = json.loads(out.getvalue())
commits = asyncio.run(commit_rows(result["cognee"]["dataset"]))
print(json.dumps({"code": code, "result": result, "commits": commits}))
"""
)


def _keyless() -> dict[str, str]:
    # LOG_LEVEL: in-process main() calls set it, and the subprocesses must start without it
    skip = {"LLM_API_KEY", "OPENAI_API_KEY", "EMBEDDING_API_KEY", "LOG_LEVEL"}
    return {k: v for k, v in os.environ.items() if k not in skip}


def _python(script: str, home: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", script, str(home), *argv],
        capture_output=True,
        text=True,
        check=False,
        env=_keyless(),
    )


def _remember(home: Path, repo: Path, *extra: str) -> dict[str, Any]:
    """The CLI's JSON result plus ``commits``, the commit rows' text in Cognee's graph."""
    done = _python(_CLI_COMMITS, home, *_args(repo, "--json", "--remember", *extra))
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["code"] == 0
    result: dict[str, Any] = out["result"] | {"commits": out["commits"]}
    return result


@pytest.mark.integration
def test_remember_reports_rows_and_links(repo: Path, tmp_path: Path) -> None:
    home = tmp_path / "cognee"
    result = _remember(home, repo, "--ci", str(CI))
    result.pop("commits")
    assert set(result) == BLAST_KEYS | {"cognee"}
    assert result["cognee"]["dataset"].startswith("gitgraph-")
    assert len(result["cognee"]["dataset"]) == len("gitgraph-") + 12
    assert result["cognee"]["rows"] == {
        "commits": 8,
        "file_changes": 15,
        "symbols": 8,
        "references": 10,
        "ci_failures": 2,
    }
    assert result["cognee"]["links"]["sha_references_commits"] == 15

    text = _python(_CLI, tmp_path / "cognee2", *_args(repo, "--ci", str(CI), "--remember"))
    assert text.returncode == 0, text.stderr
    assert "Cognee graph" in text.stdout
    assert "commits: 8" in text.stdout
    assert FROM_GRAPH in text.stdout
    assert "differs from" not in text.stderr


@pytest.mark.integration
def test_remember_blast_is_read_from_the_cognee_graph(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = _json(capsys, repo, "--ci", str(CI))
    walked = _remember(tmp_path / "cognee", repo, "--ci", str(CI))

    assert walked["computed_from"] == "cognee graph"
    for key in ("changed_symbols", "symbols", "files", "services", "co_changed", "ci_failures"):
        assert walked[key] == rows[key], key
    assert {s["name"]: s["hops"] for s in walked["services"]} == {
        "billing": 1,
        "orders": 2,
        "notifications": 4,
    }


@pytest.mark.integration
def test_remember_twice_does_not_duplicate_rows(repo: Path, tmp_path: Path) -> None:
    home = tmp_path / "cognee"
    first = _remember(home, repo)
    second = _remember(home, repo)

    assert first["cognee"]["rows"] == second["cognee"]["rows"]
    assert second["cognee"]["rows"]["commits"] == 8


@pytest.mark.integration
def test_new_head_commit_reaches_the_graph(repo: Path, tmp_path: Path) -> None:
    home = tmp_path / "cognee"
    one = _remember(home, repo)
    assert len(one["commits"]) == 8
    assert any("rotate token keys" in text for text in one["commits"])
    assert not any("hotfix the rotation" in text for text in one["commits"])

    git = Repo(repo)
    git.heads["feature/rotate-tokens"].checkout()
    (repo / "shared" / "extra.py").write_text("X = 1\n")
    git.index.add(["shared/extra.py"])
    git.index.commit("hotfix the rotation")
    git.heads["main"].checkout()

    two = _remember(home, repo)

    assert one["cognee"]["dataset"] != two["cognee"]["dataset"]
    assert two["cognee"]["rows"]["commits"] == 9
    assert any("hotfix the rotation" in text for text in two["commits"])


# demo: a subprocess, because it points cognee at its own temp home


@pytest.mark.integration
def test_demo_json_in_a_fresh_process() -> None:
    script = Path(sys.executable).parent / "cognee-gitgraph"
    done = subprocess.run(
        [str(script), "demo", "--json"],
        capture_output=True,
        text=True,
        check=False,
        env=_keyless(),
    )

    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout)
    assert {s["name"]: s["hops"] for s in result["services"]} == {
        "billing": 1,
        "notifications": 4,
        "orders": 2,
    }
    assert result["cognee"]["rows"] == {
        "commits": 8,
        "file_changes": 15,
        "symbols": 8,
        "references": 10,
        "ci_failures": 2,
    }
    assert sorted(f["job"] for f in result["ci_failures"]) == ["billing-tests", "orders-tests"]


@pytest.mark.integration
def test_demo_keeps_cognee_logs_off_the_terminal() -> None:
    done = subprocess.run(
        [_script(), "demo"], capture_output=True, text=True, check=False, env=_keyless()
    )

    assert done.returncode == 0, done.stderr
    assert "Cognee graph" in done.stdout
    for noise in ("Logging initialized", "auth posture", "Log file created", "Cognee 1.0"):
        assert noise not in done.stderr
    assert done.stderr == ""


@pytest.mark.integration
def test_verbose_brings_cognee_logs_back() -> None:
    done = subprocess.run(
        [_script(), "demo", "--verbose"],
        capture_output=True,
        text=True,
        check=False,
        env=_keyless(),
    )

    assert done.returncode == 0, done.stderr
    assert "Log file created" in done.stderr


@pytest.mark.integration
def test_demo_text_report() -> None:
    script = Path(sys.executable).parent / "cognee-gitgraph"
    done = subprocess.run(
        [str(script), "demo"], capture_output=True, text=True, check=False, env=_keyless()
    )

    assert done.returncode == 0, done.stderr
    for name in ("billing", "orders", "notifications"):
        assert name in done.stdout
    assert "search" not in done.stdout


def _history_repo(tmp_path: Path) -> Repo:
    repo = Repo.init(tmp_path / "r", initial_branch="main")
    root = Path(repo.working_dir)
    body = "".join(f"def f{i}():\n    return {i}\n" for i in range(6))  # lines 3-14
    (root / "m.py").write_text("import os\n\n" + body)
    (root / "notes.txt").write_text("a\n")
    repo.index.add(["m.py", "notes.txt"])
    repo.index.commit("base")
    repo.create_head("feat").checkout()
    lines = (root / "m.py").read_text().splitlines(keepends=True)
    lines[3] = "    return 99\n"  # line 4, in f0
    del lines[8:10]  # pure deletion: head side is empty after line 8
    (root / "m.py").write_text("".join(lines))
    (root / "notes.txt").write_text("b\n")
    repo.index.add(["m.py", "notes.txt"])
    repo.index.commit("change")
    return repo


def test_changed_items_are_head_side_ranges_for_python_only(tmp_path: Path) -> None:
    repo = _history_repo(tmp_path)
    items = cli._changed_items(Path(repo.working_dir), "main", "feat")
    assert sorted(items) == ["m.py:4-4", "m.py:8-8", "notes.txt"]


def test_python_file_with_no_hunks_counts_whole(tmp_path: Path) -> None:
    repo = Repo.init(tmp_path / "r", initial_branch="main")
    root = Path(repo.working_dir)
    (root / "a.py").write_text("def a():\n    pass\n")
    repo.index.add(["a.py"])
    repo.index.commit("base")
    repo.create_head("feat").checkout()
    repo.git.update_index("--chmod=+x", "a.py")
    repo.index.commit("mode only")
    assert cli._changed_items(root, "main", "feat") == ["a.py"]


def test_text_report_caps_co_changed_at_ten() -> None:
    from cognee_community_connector_gitgraph.cli import _report

    co = [{"path": f"f{i:02d}.py", "shas": ["1"]} for i in range(13)]
    result: dict[str, Any] = {
        "base": "a",
        "head": "b",
        "max_hops": 1,
        "changed_files": [],
        "changed_symbols": [],
        "services": [],
        "symbols": [],
        "files": [],
        "co_changed": co,
        "ci_failures": [],
        "computed_from": "rows",
    }
    report = _report(result)
    assert "f09.py" in report and "f10.py" not in report
    assert "... and 3 more" in report
    result["co_changed"] = co[:10]
    assert "more" not in _report(result)
