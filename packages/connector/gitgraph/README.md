# GitGraph connector for Cognee

Load a git repository into Cognee's graph: commits, file changes, Python symbols and references, and CI test failures from JUnit XML. From that data it computes the blast radius of a pull request: which functions call the code you changed, how many hops away they are, which top-level services they live in, and which CI failures belong to those services.

Nothing here calls an LLM. The rows are extracted from git, Python's `ast` module and JUnit XML, and they are loaded as a relational manifest: each table row becomes a node linked to its table node, with no per-row text to embed or summarise. That keeps a sync deterministic, cheap and key-free. Cognee's own ingestion still runs its local embedding model and graph extraction step, which need no API key but download local models on the first run.

Proposal: <https://github.com/topoteretes/cognee/issues/6329>

## Install

From this repository, with Python 3.11 to 3.13 and [uv](https://docs.astral.sh/uv/):

```bash
cd packages/connector/gitgraph
uv sync --locked --group dev
```

The package needs `cognee>=1.6.5,<2` (Cognee's document-mode source support) and `dlt[sqlalchemy]>=1.16,<2`. The lockfile records the tested resolution. No core changes or database adapter registration are needed.

## Inputs

```python
import asyncio

import cognee

from cognee_community_connector_gitgraph import gitgraph_source, link_rows

source = gitgraph_source("path/to/repo", ci_logs="path/to/junit-xml-dir")


async def main() -> None:
    result = await cognee.remember(source, dataset_name="gitgraph", self_improvement=False)
    print(result.status)
    print(await link_rows(source))  # row-to-row edges, e.g. {"sha_references_commits": 15, ...}


asyncio.run(main())
```

`gitgraph_source(repo_path, refs=None, ci_logs=None, symbols_ref=None)` returns a dlt source named `gitgraph`:

- `repo_path`: the git repository.
- `refs`: the branches to read history from (default: all local branches).
- `symbols_ref`: the ref whose tree gives the symbols (default: the first of `refs`, else `HEAD`).
- `ci_logs`: a JUnit XML file, a directory of them, or a list of either. Without it there is no `ci_failures` table.

The repo, refs and CI paths are read when the source is built, so a bad one raises `ValueError` there.

`await link_rows(source)` writes the row-to-row edges after `cognee.remember`. Cognee links each row to its table node but, for a dlt source, to no other row. `link_rows` adds them, named `{column}_references_{table}`, and returns the number written per relationship plus `"missing"` and `"ambiguous"` counts for edges it had to skip.

A runnable version is in `examples/example.py`: it remembers a generated sample repo in a temp Cognee home, prints the row count per table and the links written. Pass a repo path to use your own: `uv run python examples/example.py /path/to/repo --ci-logs /path/to/junit`.

## What lands in Cognee

Five tables. Each has a single `id` column as its primary key, and `id` is the first column of every row.

| Table | `id` | Other columns |
| --- | --- | --- |
| `commits` | commit sha | `sha`, `parents`, `author`, `author_email`, `date`, `message`, `refs` (lists are space-separated strings) |
| `file_changes` | `<sha>:<path>` | `sha`, `path`, `change_type`, `lines_added`, `lines_deleted`, `old_path` |
| `symbols` | `<file>::<qualified name>` | `kind` (function, class, method), `name`, `file`, `line`, `end_line` |
| `references` | `<file>:<owner>-><target>:<kind>` | `from_file`, `from_symbol`, `to_symbol`, `kind` (call, import), `line` |
| `ci_failures` | `<job>:<test id>` | `job`, `test_id`, `file`, `commit_sha`, `kind`, `message` |

## Repeated syncs

Every table uses dlt's `replace` write disposition with `id` as the primary key, so remembering the same source twice adds no new row nodes and running `link_rows` again adds no new edges.

`cognee.remember` runs Cognee's improvement step by default (`self_improvement=True`). Pass `self_improvement=False`, as the example does, so a repeated sync only reloads the rows.

To drop a dataset, use `cognee.forget(dataset="gitgraph")`.

## Blast radius

`blast_radius(tables, changed, max_hops=4)` is a pure function over the extracted rows (graph traversal with NetworkX, no LLM). `tables` maps a table name to its rows, for example from `extract_commits` and `extract_symbols`, and `changed` is a list of repo paths (every symbol in the file changes), `path:line` items or `path:start-end` line ranges (the innermost symbols the range overlaps). It returns JSON-serialisable data with `changed_files`, `changed_symbols`, `symbols`, `files`, `services`, `co_changed` and `ci_failures`, every list sorted.

## CLI

The package installs a `cognee-gitgraph` command:

```bash
cognee-gitgraph blast --repo PATH --base REF --head REF [--ci FILE_OR_DIR]... [--hops N] [--json] [--remember] [--verbose] [--html PATH]
cognee-gitgraph demo [--json] [--verbose] [--html PATH]
```

- `--ci` may be repeated; each is a JUnit XML file or a directory of them.
- `--hops` is how many caller hops to follow (default 4).
- `--json` prints the result as JSON instead of the text report.
- `--remember` also loads the repo into Cognee (with `self_improvement=False`) and links the rows, using one dataset per head commit named `gitgraph-<first 12 characters of the head sha>`. Cognee's progress output is hidden unless `--verbose` is given.
- `--html PATH` also writes a self-contained 3D page of the blast radius: red nodes are the changed symbols, amber nodes are at risk, grey nodes are safe. The 3d-force-graph library (MIT) is inlined from `cognee_community_connector_gitgraph/vendor/`, so the page makes no network request.
- The diff is `base...head`. Symbols are read from `head`; history is read from both refs. A symbol counts as changed when one of the diff's changed line ranges on `head` overlaps its lines.
- Co-changed files leave out commits that touch more than 20 files. The text report shows the top 10; `--json` lists them all.
- A bad repo, ref or CI path prints `error: ...` to stderr and exits with status 2.

`cognee-gitgraph demo` builds a small sample monorepo and blasts a branch of it.

## Tests

```bash
uv run pytest tests -ra
uv run ruff check .
uv run ruff format --check .
```

The tests need no API key. They need the network only on the first run, for Cognee's local models (fastembed and GLiNER, plus CPU-only PyTorch).

## Limitations

- Symbols come from Python files only. Other languages are not parsed.
- Only top-level functions, top-level classes and their direct methods become symbols; nested functions and classes are skipped. Calls are resolved by name through imports, plus `self.m()`/`cls.m()` inside a class, `Cls.m()`, and `obj.m()` when every assignment to `obj` in the same function is `Cls(...)` of one class. Attribute chains like `self.client.m()`, dynamic calls and type inference across functions are not seen.
- A symbol spans its `def` or `class` line to its last line, so an edit that only touches a decorator marks no symbol as changed. A plain `path:line` item given to `blast_radius` still maps to the nearest symbol that starts at or before the line.
- `link_rows` reads the whole graph to find the row nodes, so it gets slower as the graph grows. A row key claimed by more than one node is not linked and is counted as `ambiguous`.
- Merge commits produce no `file_changes` rows.

## License

Apache-2.0, same as Cognee. The vendored 3d-force-graph library keeps its own MIT license in `cognee_community_connector_gitgraph/vendor/LICENSE.txt`.
