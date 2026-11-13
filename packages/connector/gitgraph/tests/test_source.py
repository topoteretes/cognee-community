"""The dlt source, loaded for real into a local sqlite destination."""

from pathlib import Path
from typing import Any

import dlt
import pytest
from git import Repo

from cognee_community_connector_gitgraph import SampleRepo, build_sample_repo, gitgraph_source
from cognee_community_connector_gitgraph.ci_logs import parse_ci_logs
from cognee_community_connector_gitgraph.git_history import extract_commits
from cognee_community_connector_gitgraph.symbols import extract_symbols

FIXTURES = Path(__file__).parent / "fixtures" / "ci"
FEATURE = "feature/rotate-tokens"
REFS = ["main", FEATURE]
TABLES = ["commits", "file_changes", "symbols", "references", "ci_failures"]

Rows = dict[str, list[tuple[Any, ...]]]


@pytest.fixture
def sample(tmp_path: Path) -> SampleRepo:
    return build_sample_repo(tmp_path / "repo")


def _pipeline(tmp_path: Path, name: str) -> dlt.Pipeline:
    return dlt.pipeline(
        pipeline_name=name,
        destination=dlt.destinations.sqlalchemy(credentials=f"sqlite:///{tmp_path}/{name}.db"),
        dataset_name="gitgraph",
        pipelines_dir=str(tmp_path / "pipelines"),
    )


def _data_tables(pipeline: dlt.Pipeline) -> list[str]:
    return sorted(t for t in pipeline.default_schema.tables if not t.startswith("_dlt"))


def _names(pipeline: dlt.Pipeline, table: str) -> list[str]:
    """The table's columns without dlt's bookkeeping ones."""
    columns = pipeline.default_schema.tables[table]["columns"]
    return [c for c in columns if not c.startswith("_dlt")]


def _read(pipeline: dlt.Pipeline) -> Rows:
    """Every row of every data table, without dlt's bookkeeping columns."""
    out: Rows = {}
    with pipeline.sql_client() as client:
        for table in _data_tables(pipeline):
            columns = _names(pipeline, table)
            names = ", ".join(client.escape_column_name(c) for c in columns)
            query = f"SELECT {names} FROM {client.make_qualified_table_name(table)}"
            fetched = [tuple(r) for r in client.execute_sql(query) or []]
            out[table] = sorted(fetched, key=lambda r: [(v is None, str(v)) for v in r])
    return out


def _load(tmp_path: Path, name: str, repo: Path, **kwargs: Any) -> tuple[dlt.Pipeline, Rows]:
    pipeline = _pipeline(tmp_path, name)
    pipeline.run(gitgraph_source(repo, **kwargs))
    return pipeline, _read(pipeline)


def _column(rows: Rows, pipeline: dlt.Pipeline, table: str, name: str) -> list[Any]:
    index = _names(pipeline, table).index(name)
    return [r[index] for r in rows[table]]


def test_source_name_is_gitgraph(sample: SampleRepo) -> None:
    assert gitgraph_source(sample.path).name == "gitgraph"


def test_sample_row_counts(tmp_path: Path, sample: SampleRepo) -> None:
    pipeline, rows = _load(tmp_path, "counts", sample.path, refs=REFS, ci_logs=FIXTURES)
    git = extract_commits(sample.path, REFS)
    syms = extract_symbols(sample.path, "main")
    ci = parse_ci_logs([FIXTURES])
    assert {t: len(r) for t, r in rows.items()} == {
        "commits": len(git["commits"]),
        "file_changes": len(git["file_changes"]),
        "symbols": len(syms["symbols"]),
        "references": len(syms["references"]),
        "ci_failures": len(ci["ci_failures"]),
    }
    assert {t: len(r) for t, r in rows.items()} == {
        "commits": 8,
        "file_changes": 15,
        "symbols": 8,
        "references": 10,
        "ci_failures": 2,
    }
    assert _data_tables(pipeline) == sorted(TABLES)
    assert not [t for t in _data_tables(pipeline) if "__" in t]


def test_commit_rows_are_flat(tmp_path: Path, sample: SampleRepo) -> None:
    pipeline, rows = _load(tmp_path, "flat", sample.path, refs=REFS)
    commits = extract_commits(sample.path, REFS)["commits"]
    by_sha = {c["sha"]: c for c in commits}
    assert sorted(_column(rows, pipeline, "commits", "id")) == sorted(by_sha)
    assert sorted(_column(rows, pipeline, "commits", "sha")) == sorted(by_sha)
    ids = _column(rows, pipeline, "commits", "id")
    parents = dict(zip(ids, _column(rows, pipeline, "commits", "parents"), strict=True))
    refs = dict(zip(ids, _column(rows, pipeline, "commits", "refs"), strict=True))
    for sha, commit in by_sha.items():
        assert parents[sha] == " ".join(commit["parents"])
        assert refs[sha] == " ".join(commit["refs"])


def test_primary_key_is_id_and_ids_unique(tmp_path: Path, sample: SampleRepo) -> None:
    pipeline, rows = _load(tmp_path, "pk", sample.path, refs=REFS, ci_logs=FIXTURES)
    for table in TABLES:
        columns = pipeline.default_schema.tables[table]["columns"]
        assert [c for c, spec in columns.items() if spec.get("primary_key")] == ["id"], table
        ids = _column(rows, pipeline, table, "id")
        assert len(ids) == len(set(ids)) > 0, table
    changes = _column(rows, pipeline, "file_changes", "id")
    assert all(":" in i for i in changes)
    root = extract_commits(sample.path, REFS)["commits"][0]["sha"]
    assert f"{root}:shared/__init__.py" in changes


def test_replace_disposition(sample: SampleRepo) -> None:
    source = gitgraph_source(sample.path, refs=REFS, ci_logs=FIXTURES)
    assert sorted(source.resources) == sorted(TABLES)
    for resource in source.resources.values():
        assert resource.write_disposition == "replace"


def test_idempotent(tmp_path: Path, sample: SampleRepo) -> None:
    pipeline, first = _load(tmp_path, "twice", sample.path, refs=REFS, ci_logs=FIXTURES)
    pipeline.run(gitgraph_source(sample.path, refs=REFS, ci_logs=FIXTURES))
    second = _read(pipeline)
    assert first == second
    assert sum(len(r) for r in second.values()) == 8 + 15 + 8 + 10 + 2


def test_forget_on_delete(tmp_path: Path, sample: SampleRepo) -> None:
    pipeline, before = _load(tmp_path, "forget", sample.path, refs=REFS)
    assert "search/index.py::search" in _column(before, pipeline, "symbols", "id")
    repo = Repo(sample.path)
    repo.index.remove(["search/index.py"], working_tree=True)
    repo.index.commit("drop search")
    pipeline.run(gitgraph_source(sample.path, refs=REFS))
    after = _read(pipeline)
    symbol_ids = _column(after, pipeline, "symbols", "id")
    assert "search/index.py::search" not in symbol_ids
    assert len(symbol_ids) == 7
    assert not [i for i in _column(after, pipeline, "references", "id") if "search/" in i]
    assert len(after["references"]) == 10
    assert len(after["commits"]) == len(before["commits"]) + 1
    messages = _column(after, pipeline, "commits", "message")
    assert "drop search" in messages
    assert "drop search" not in _column(before, pipeline, "commits", "message")


def test_symbols_ref_defaults_to_first_ref_then_head(tmp_path: Path, sample: SampleRepo) -> None:
    repo = Repo(sample.path)
    repo.heads[FEATURE].checkout()
    (sample.path / "extra.py").write_text("def extra():\n    pass\n")
    repo.index.add(["extra.py"])
    repo.index.commit("add extra")
    repo.heads[sample.base].checkout()  # HEAD is main, which lacks extra.py
    extra = "extra.py::extra"

    def has_extra(name: str, **kwargs: Any) -> bool:
        pipeline, rows = _load(tmp_path, name, sample.path, **kwargs)
        return extra in _column(rows, pipeline, "symbols", "id")

    assert has_extra("ref-first", refs=[FEATURE, "main"])
    assert not has_extra("ref-main-first", refs=["main", FEATURE])
    assert not has_extra("ref-head")
    assert has_extra("ref-explicit", refs=REFS, symbols_ref=FEATURE)


def test_no_ci_logs_means_no_ci_table(tmp_path: Path, sample: SampleRepo) -> None:
    source = gitgraph_source(sample.path, refs=REFS)
    assert "ci_failures" not in source.resources
    pipeline, rows = _load(tmp_path, "no-ci", sample.path, refs=REFS)
    assert "ci_failures" not in rows
    assert _data_tables(pipeline) == sorted(t for t in TABLES if t != "ci_failures")


def test_ci_logs_accepts_single_path_and_list(sample: SampleRepo) -> None:
    two = [FIXTURES / "orders-tests.xml", FIXTURES / "billing-tests.xml"]
    for ci in (FIXTURES, str(FIXTURES), two):
        source = gitgraph_source(sample.path, refs=REFS, ci_logs=ci)
        assert "ci_failures" in source.resources


def test_nullable_columns_exist_in_schema(tmp_path: Path, sample: SampleRepo) -> None:
    pipeline, rows = _load(tmp_path, "nullable", sample.path, refs=REFS, ci_logs=FIXTURES)
    tables = pipeline.default_schema.tables
    for table, column in [
        ("file_changes", "old_path"),
        ("references", "from_symbol"),
        ("ci_failures", "file"),
        ("ci_failures", "commit_sha"),
    ]:
        assert column in tables[table]["columns"], (table, column)
        assert tables[table]["columns"][column].get("nullable", True) is True
    # old_path is None in every sample row: the hint is what keeps the column.
    assert set(_column(rows, pipeline, "file_changes", "old_path")) == {None}
    assert None in _column(rows, pipeline, "references", "from_symbol")


def test_all_null_ci_columns_kept(tmp_path: Path, sample: SampleRepo) -> None:
    # Only the billing failure: file and commit_sha are None in every ci row.
    pipeline, rows = _load(
        tmp_path, "all-null", sample.path, refs=REFS, ci_logs=[FIXTURES / "billing-tests.xml"]
    )
    columns = pipeline.default_schema.tables["ci_failures"]["columns"]
    assert "file" in columns and "commit_sha" in columns
    assert set(_column(rows, pipeline, "ci_failures", "file")) == {None}
    assert set(_column(rows, pipeline, "ci_failures", "commit_sha")) == {None}


def test_skipped_files_are_logged_not_loaded(
    tmp_path: Path, sample: SampleRepo, caplog: pytest.LogCaptureFixture
) -> None:
    repo = Repo(sample.path)
    (sample.path / "broken.py").write_text("def (:\n")
    repo.index.add(["broken.py"])
    repo.index.commit("add broken")
    bad = tmp_path / "bad.xml"
    bad.write_text("<nope")
    with caplog.at_level("WARNING"):
        gitgraph_source(sample.path, refs=["main"], ci_logs=[FIXTURES, bad])
    text = caplog.text
    assert "broken.py" in text
    assert str(bad) in text


def test_bad_repo_raises_value_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a git repository"):
        gitgraph_source(tmp_path / "missing")


def test_unknown_ref_raises_value_error(sample: SampleRepo) -> None:
    with pytest.raises(ValueError, match="unknown ref"):
        gitgraph_source(sample.path, refs=["nope"])
    with pytest.raises(ValueError, match="unknown ref"):
        gitgraph_source(sample.path, refs=REFS, symbols_ref="nope")


def test_missing_ci_path_raises_value_error(sample: SampleRepo, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no such path"):
        gitgraph_source(sample.path, ci_logs=tmp_path / "missing.xml")
