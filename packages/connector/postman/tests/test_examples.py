import importlib.util
import os
import sys
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).parent / "support"


def load_example(name):
    spec = importlib.util.spec_from_file_location(name, EXAMPLES / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_demo_overrides_inherited_backend_roots(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SYSTEM_ROOT_DIRECTORY", "/unrelated")
    monkeypatch.setenv("DB_PATH", "/unrelated/database")
    monkeypatch.setenv("GRAPH_FILE_PATH", "/unrelated/graph")
    monkeypatch.setenv("VECTOR_DB_URL", "/unrelated/vector")
    monkeypatch.setenv("CACHE_DB_URL", "postgresql://unrelated/cache")
    previous_umask = os.umask(0o077)
    try:
        runtime = load_example("local_runtime").configure_runtime(tmp_path / "demo")
    finally:
        os.umask(previous_umask)
    assert os.environ["SYSTEM_ROOT_DIRECTORY"] == str(runtime / "system")
    assert os.environ["DB_PATH"].startswith(str(runtime))
    assert os.environ["CACHE_DB_URL"].endswith(str(runtime / "cache.db"))
    assert "GRAPH_FILE_PATH" not in os.environ and "VECTOR_DB_URL" not in os.environ
    assert Path.cwd() == runtime


def test_verifier_detects_stale_records_in_each_store(monkeypatch):
    monkeypatch.syspath_prepend(str(EXAMPLES))
    sys.modules.pop("local_runtime", None)
    verifier = load_example("verify_stores")
    stats = {"data_rows": 0, "graph_nodes": 0, "graph_edges": 0, "vector_rows": {"chunks": 0}}
    texts = {"Data": "", "graph": "", "vectors": ""}
    verifier.assert_stores(stats, texts, expect_empty=True)
    for store in texts:
        with pytest.raises(RuntimeError, match="Obsolete text"):
            verifier.assert_stores(stats, {**texts, store: "obsolete"}, forbidden=["obsolete"])
    for field in ("data_rows", "graph_nodes", "graph_edges"):
        with pytest.raises(RuntimeError, match="stale records"):
            verifier.assert_stores({**stats, field: 1}, texts, expect_empty=True)
    with pytest.raises(RuntimeError, match="stale records"):
        verifier.assert_stores({**stats, "vector_rows": {"chunks": 1}}, texts, expect_empty=True)


def test_dlt_runtime_isolation_and_same_runtime_recovery(tmp_path):
    """Fresh example processes must not inherit another runtime's cache/packages."""
    import subprocess

    code = r"""
import json, sys
from pathlib import Path
from local_runtime import configure_pipeline, configure_runtime
runtime = configure_runtime(Path(sys.argv[1]))
import dlt
from dlt.destinations import sqlalchemy
from cognee.tasks.ingestion.dlt_utils import pipeline_name_for_source
from cognee_community_connector_postman import postman_source
from test_postman import FakeClient
source = postman_source(resource_name='postman', client=FakeClient(), workspace_id='w',
                        account_id='1', corpus_owner='owner', dataset_name='dataset')
name = pipeline_name_for_source(source, 'dataset')
from dlt.common.runtime import run_context
from dlt.common.configuration.container import Container
from dlt.common.configuration.specs.pluggable_run_context import PluggableRunContext
global_config = runtime.parent / 'synthetic-global'
global_config.mkdir(exist_ok=True)
(global_config / 'config.toml').write_text(
    f'[{name}]\n pipelines_dir = "{runtime.parent / "foreign-named-pipelines"}"\n')
run_context.active()._global_dir = str(global_config)
Container()[PluggableRunContext].reload_providers()
configure_pipeline(source, 'dataset', runtime)
pipeline = dlt.pipeline(pipeline_name=name,
                       destination=sqlalchemy(credentials=f'sqlite:///{runtime}/staging.sqlite'),
                       dataset_name='dataset')
assert Path(pipeline.working_dir).is_relative_to(runtime / 'dlt/pipelines')
cache = pipeline.state.get('sources', {}).get(source.name, {}).get('postman_snapshot', {})
rows = sum(len(c['rows']) for c in cache.get('collections', {}).values())
pending = len(pipeline.list_normalized_load_packages())
assert rows == int(sys.argv[2]), (rows, sys.argv)
assert pending == int(sys.argv[3]), (pending, sys.argv)
if sys.argv[4] == 'seed':
    pipeline.extract(source)
    pipeline.normalize()
    assert len(pipeline.list_normalized_load_packages()) == 1
print(json.dumps({'working_dir': pipeline.working_dir, 'cached_rows': rows, 'pending': pending}))
"""
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": os.pathsep.join(
                [str(EXAMPLES), str(Path(__file__).parent), str(Path(__file__).parents[1])]
            ),
            "DLT_DATA_DIR": str(tmp_path / "foreign-data"),
            "DLT_PROJECT_DIR": str(tmp_path / "foreign-project"),
            "DLT_LOCAL_DIR": str(tmp_path / "foreign-local"),
            "DLT_CONFIG_FOLDER": str(tmp_path / "foreign-config"),
            "PIPELINES_DIR": str(tmp_path / "foreign-pipelines"),
            "PIPELINES__PIPELINES_DIR": str(tmp_path / "foreign-nested-pipelines"),
            "PIPELINES__INGEST_DLT_FAKE__PIPELINES_DIR": str(tmp_path / "foreign-scoped-pipelines"),
            "COGNEE_LOG_FILE": "false",
            "RUNTIME__DLTHUB_TELEMETRY": "false",
        }
    )
    project = tmp_path / "foreign-project" / ".dlt"
    project.mkdir(parents=True)
    (project / "config.toml").write_text(f'pipelines_dir = "{tmp_path / "foreign-toml"}"\n')

    def run(name, rows, pending, action):
        result = subprocess.run(
            [sys.executable, "-c", code, str(tmp_path / name), str(rows), str(pending), action],
            env=env,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    run("runtime-a", 0, 0, "seed")
    run("runtime-a", 2, 1, "inspect")
    run("runtime-b", 0, 0, "inspect")
    run("runtime-a", 2, 1, "inspect")
    assert not (tmp_path / "foreign-data").exists()
    assert not (tmp_path / "foreign-pipelines").exists()
    assert not (tmp_path / "foreign-named-pipelines").exists()
