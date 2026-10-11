import importlib.util
import os
import sys
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).parents[1] / "examples"


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
