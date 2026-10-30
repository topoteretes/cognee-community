"""``cognee-gitgraph``: the blast radius of a branch, and a one-command demo."""

import argparse
import asyncio
import contextlib
import io
import json
import os
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from git import GitCommandError, Repo

from .blast import blast_radius, changed_symbol_ids
from .ci_logs import parse_ci_logs
from .git_history import extract_commits
from .html import render_html
from .sample import build_sample_repo
from .symbols import extract_symbols

# The demo ships its CI logs as strings: the wheel carries no fixture files.
_DEMO_CI = {
    "billing-tests.xml": (
        '<testsuites><testsuite name="pytest">'
        '<testcase classname="billing.test_invoice" name="test_charge_valid_token"/>'
        '<testcase classname="billing.test_invoice" name="test_charge_with_max_age">'
        "<error message=\"TypeError: charge() got an unexpected keyword argument 'max_age'\">"
        "TypeError</error></testcase></testsuite></testsuites>"
    ),
    "orders-tests.xml": (
        '<testsuites><testsuite name="pytest">'
        '<testcase classname="orders.test_checkout" name="test_checkout_returns_cart"'
        ' file="orders/test_checkout.py" line="5"/>'
        '<testcase classname="orders.test_checkout" name="test_checkout_rejects_bad_token"'
        ' file="orders/test_checkout.py" line="11">'
        '<failure message="assert False is True">AssertionError</failure>'
        "</testcase></testsuite></testsuites>"
    ),
}


_HUNK = re.compile(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
# ponytail: the text report shows the top 10 co-changed files; the JSON keeps them all.
_TOP_CO_CHANGED = 10


def _hops(count: int) -> str:
    return f"{count} hop" if count == 1 else f"{count} hops"


def _changed_paths(repo: Path, base: str, head: str) -> list[str]:
    try:
        out = Repo(repo).git.diff("--name-only", "-z", f"{base}...{head}")
    except GitCommandError as exc:
        raise ValueError(f"cannot diff {base}...{head}") from exc
    return [path for path in out.split("\0") if path]


def _changed_items(repo: Path, base: str, head: str) -> list[str]:
    """Changed items for ``blast_radius``: ``path:start-end`` per head-side hunk of a .py file.

    A pure deletion (``+a,0``) touches line ``a``. Other files, and .py files git shows no hunk
    for (mode change, binary), stay plain paths: every symbol in them counts as changed.
    """
    items: list[str] = []
    git = Repo(repo).git
    for path in _changed_paths(repo, base, head):
        hunks: list[tuple[int, int]] = []
        if path.endswith(".py"):
            # ponytail: one git call per changed .py file; batch into one diff if that gets slow.
            diff = git.diff("-U0", "--no-color", f"{base}...{head}", "--", f":(literal){path}")
            for match in _HUNK.finditer(diff):
                start, count = int(match[1]), int(match[2] or 1)
                hunks.append((max(start, 1), max(start, 1) + max(count, 1) - 1))
        items += [f"{path}:{a}-{b}" for a, b in hunks] or [path]
    return items


def _blast(args: argparse.Namespace) -> dict[str, Any]:
    refs = [args.base, args.head]
    history = extract_commits(args.repo, refs)  # first, so a bad repo or ref fails here
    code = extract_symbols(args.repo, args.head)
    tables = {
        "commits": history["commits"],
        "file_changes": history["file_changes"],
        "symbols": code["symbols"],
        "references": code["references"],
    }
    if args.ci:
        tables["ci_failures"] = parse_ci_logs(args.ci)["ci_failures"]
    changed = _changed_items(args.repo, args.base, args.head)
    # Resolved once from the rows: Cognee's graph has no line info to resolve ranges with.
    ids = changed_symbol_ids(code["symbols"], changed)
    result = blast_radius(tables, changed, max_hops=args.hops, changed_symbols=ids)
    result |= {"computed_from": "rows"}
    if args.remember:
        sha = Repo(args.repo).commit(args.head).hexsha[:12]
        # cognee prints progress; stdout is the report, so it goes to stderr (or nowhere)
        with contextlib.redirect_stdout(sys.stderr if args.verbose else io.StringIO()):
            summary, graph = asyncio.run(_remember(args, refs, f"gitgraph-{sha}"))
        # The at-risk walk is read back from Cognee's graph; history and CI still come from rows.
        tables |= graph
        walked = blast_radius(tables, changed, max_hops=args.hops, changed_symbols=ids)
        if any(walked[key] != result[key] for key in ("symbols", "files", "services")):
            print("warning: the blast over Cognee's graph differs from the rows'", file=sys.stderr)
        result = walked | {"computed_from": "cognee graph", "cognee": summary}
    result |= {"base": args.base, "head": args.head}
    if args.html:  # here, not in main: the demo's tables die with its temp dir
        args.html.write_text(render_html(tables, result), encoding="utf-8")
        print(f"wrote {args.html}", file=sys.stderr)
    return result


async def _remember(
    args: argparse.Namespace, refs: list[str], dataset: str
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """Remember the repo and link its rows; returns the summary and the graph's symbol tables."""
    import cognee
    from cognee.infrastructure.databases.graph import get_graph_engine

    from .links import graph_tables, link_rows
    from .source import gitgraph_source

    source = gitgraph_source(args.repo, refs=refs, ci_logs=args.ci or None)
    done = await cognee.remember(source, dataset_name=dataset, self_improvement=False)
    if done.status != "completed":
        raise ValueError(f"cognee could not remember the repo: {done.error}")
    links = await link_rows(source)
    nodes, edges = await (await get_graph_engine()).get_graph_data()
    tables = {str(node_id): props["name"] for node_id, props in nodes}
    rows = Counter(tables[str(target)] for _, target, name, _ in edges if name == "is_row_of")
    summary = {"dataset": dataset, "rows": dict(sorted(rows.items())), "links": links}
    return summary, graph_tables(nodes, edges)


def _report(result: dict[str, Any]) -> str:
    lines = [
        f"Blast radius of {result['base']}...{result['head']} (up to {_hops(result['max_hops'])})",
        f"Changed files ({len(result['changed_files'])}): " + ", ".join(result["changed_files"]),
        f"Changed symbols ({len(result['changed_symbols'])}): "
        + ", ".join(result["changed_symbols"]),
        "",
        "At-risk services:",
    ]
    lines += [
        f"  {s['name']}  {_hops(s['hops'])}  {' -> '.join(s['path'])}" for s in result["services"]
    ]
    lines += ["  none"] * (not result["services"])
    lines += [f"At-risk symbols: {len(result['symbols'])} in {len(result['files'])} files"]
    walked = "Cognee's graph" if result["computed_from"] == "cognee graph" else "the extracted rows"
    lines += [f"Callers found by walking {walked}"]
    if result["co_changed"]:
        lines += ["", "Co-changed files:"]
        lines += [
            f"  {c['path']}  ({len(c['shas'])} shared commit{'s' * (len(c['shas']) != 1)})"
            for c in result["co_changed"][:_TOP_CO_CHANGED]
        ]
        more = len(result["co_changed"]) - _TOP_CO_CHANGED
        lines += [f"  ... and {more} more"] * (more > 0)
    if result["ci_failures"]:
        lines += ["", "CI failures in at-risk services:"]
        lines += [
            f"  {f['job']}  {f['service']}  {f['test_id']}  {f['message']}"
            for f in result["ci_failures"]
        ]
    if "cognee" in result:
        graph = result["cognee"]
        lines += ["", f"Cognee graph (dataset {graph['dataset']}):"]
        lines += [f"  {table}: {count}" for table, count in graph["rows"].items()]
        lines += ["Links between rows:"]
        lines += [f"  {name}: {count}" for name, count in sorted(graph["links"].items())]
    return "\n".join(lines)


def _emit(result: dict[str, Any], as_json: bool) -> None:
    print(json.dumps(result, indent=2, sort_keys=True) if as_json else _report(result))


def _demo(args: argparse.Namespace) -> dict[str, Any]:
    import cognee

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cognee.config.system_root_directory(str(root / "system"))
        cognee.config.data_root_directory(str(root / "data"))
        ci = root / "ci"
        ci.mkdir()
        for name, xml in _DEMO_CI.items():
            (ci / name).write_text(xml)
        sample = build_sample_repo(root / "repo")
        return _blast(
            argparse.Namespace(
                repo=sample.path,
                base=sample.base,
                head=sample.head,
                ci=[ci],
                hops=4,
                remember=True,
                verbose=args.verbose,
                html=args.html,
            )
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cognee-gitgraph", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    blast = commands.add_parser("blast", help="what a branch can break")
    blast.add_argument("--repo", type=Path, required=True, help="git repository")
    blast.add_argument("--base", required=True, help="base ref, e.g. main")
    blast.add_argument("--head", required=True, help="head ref, e.g. the feature branch")
    blast.add_argument("--ci", type=Path, action="append", help="JUnit XML file or directory")
    blast.add_argument("--hops", type=int, default=4, help="caller hops to follow (default 4)")
    blast.add_argument("--remember", action="store_true", help="also load the repo into Cognee")
    demo = commands.add_parser("demo", help="blast the built-in sample repo through Cognee")
    for command in (blast, demo):
        command.add_argument("--json", action="store_true", help="print JSON only")
        command.add_argument("--verbose", action="store_true", help="show Cognee's own logs")
        command.add_argument("--html", type=Path, help="also write a self-contained 3D page")
    blast.set_defaults(run=_blast)
    demo.set_defaults(run=_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.verbose:  # cognee reads this when it is first imported, so set it before then
        os.environ.setdefault("LOG_LEVEL", "ERROR")
        with contextlib.suppress(ImportError):  # fastembed logs through loguru, not LOG_LEVEL
            from loguru import logger

            logger.disable("fastembed")
    try:
        result = args.run(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _emit(result, args.json)
    return 0
