"""The dlt source cognee ingests: commits, file changes, symbols, references and CI failures.

Shapes follow what cognee's dlt ingestion reads: it keeps one primary-key column per table, so
every table has a single unique ``id`` column, and list columns would become dlt child tables that
cognee then treats as tables of their own, so lists are flattened into space-separated strings.
"""

import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import dlt
from dlt.common.schema.typing import TColumnSchema

from .ci_logs import parse_ci_logs
from .git_history import extract_commits
from .symbols import extract_symbols

logger = logging.getLogger(__name__)

Row = dict[str, Any]


def _resource(name: str, rows: list[Row], nullable: tuple[str, ...] = ()) -> Any:
    # dlt drops a column that is None in every row unless it is declared, so the columns that can
    # be None get a type. A fresh dict per column: dlt edits hint dicts in place.
    # Declared columns come first in the table, and ``id`` must be first of all: cognee renders a
    # row's columns in order and ``link_rows`` trusts only the first line as the row's id.
    columns = {"id": TColumnSchema(data_type="text")}
    columns |= {c: TColumnSchema(data_type="text", nullable=True) for c in nullable}
    return dlt.resource(
        rows, name=name, write_disposition="replace", primary_key="id", columns=columns
    )


def _warn_skipped(what: str, skipped: list[Row]) -> None:
    if skipped:
        listing = "; ".join(f"{s['file']} ({s['reason']})" for s in skipped)
        logger.warning("skipped %d %s: %s", len(skipped), what, listing)


@dlt.source(name="gitgraph", max_table_nesting=0)
def gitgraph_source(
    repo_path: str | Path,
    refs: list[str] | None = None,
    ci_logs: str | Path | Iterable[str | Path] | None = None,
    symbols_ref: str | None = None,
) -> list[Any]:
    """Git history, symbols and CI failures of ``repo_path`` as a dlt source named ``gitgraph``.

    ``refs`` are the branches to read history from (``None`` means all local branches).
    ``symbols_ref`` is the ref whose tree gives the symbols; it defaults to the first of ``refs``,
    else ``HEAD``. ``ci_logs`` is a JUnit XML path, a directory of them, or a list of either;
    without it there is no ``ci_failures`` resource at all.

    Everything is read when the source is built, so a bad repo, ref or CI path raises
    ``ValueError`` here and not in the middle of a pipeline run.
    """
    git = extract_commits(repo_path, refs)
    # ponytail: `refs=[]` is treated as "no refs" for the symbols default, so it reads HEAD.
    syms = extract_symbols(repo_path, symbols_ref or (refs[0] if refs else "HEAD"))
    _warn_skipped("python files", syms["skipped"])

    commits = [
        {**c, "id": c["sha"], "parents": " ".join(c["parents"]), "refs": " ".join(c["refs"])}
        for c in git["commits"]
    ]
    file_changes = [{**f, "id": f"{f['sha']}:{f['path']}"} for f in git["file_changes"]]
    resources = [
        _resource("commits", commits),
        _resource("file_changes", file_changes, nullable=("old_path",)),
        _resource("symbols", syms["symbols"]),
        _resource("references", syms["references"], nullable=("from_symbol",)),
    ]
    if ci_logs is not None:
        paths = [ci_logs] if isinstance(ci_logs, str | Path) else list(ci_logs)
        ci = parse_ci_logs(paths)
        _warn_skipped("CI log files", ci["skipped"])
        nullable = ("file", "commit_sha")
        resources.append(_resource("ci_failures", ci["ci_failures"], nullable=nullable))
    return resources
