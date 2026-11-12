"""Disposable real-store acceptance worker; never run on the WDAC-blocked host.

Only the opted-in pytest wrapper starts this worker on an approved non-Windows
runner. It never prunes. Every local backend is checked before opening it, the
process receives no user credentials, and model calls use deterministic stubs.
"""

import asyncio
import json
import os
import shutil
import socket
import sqlite3
import sys
import traceback
from contextlib import ExitStack
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch
from uuid import UUID


def sandbox_environment(root):
    """Construct a child-only environment; never inherit DB/model credentials."""
    root = Path(root).resolve()
    env = {key: os.environ[key] for key in ("PATH", "LANG", "SYSTEMROOT") if key in os.environ}
    env.update(
        {
            "DATA_ROOT_DIRECTORY": str(root / "data"),
            "HOME": str(root / "home"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "SYSTEM_ROOT_DIRECTORY": str(root / "system"),
            "CACHE_ROOT_DIRECTORY": str(root / "cache"),
            "LOGS_ROOT_DIRECTORY": str(root / "logs"),
            "REPOS_ROOT_DIRECTORY": str(root / "repos"),
            "COGNEE_LOGS_DIR": str(root / "logs"),
            "COGNEE_REPOS_DIR": str(root / "repos"),
            "DB_PROVIDER": "sqlite",
            "DB_PATH": str(root / "relational"),
            "DB_NAME": "jenkins_acceptance",
            "GRAPH_DATABASE_PROVIDER": "ladybug",
            "GRAPH_DATASET_DATABASE_HANDLER": "ladybug",
            "GRAPH_FILE_PATH": str(root / "graph"),
            "GRAPH_DATABASE_SUBPROCESS_ENABLED": "false",
            "VECTOR_DB_PROVIDER": "lancedb",
            "VECTOR_DATASET_DATABASE_HANDLER": "lancedb",
            "VECTOR_DB_URL": str(root / "vector"),
            "VECTOR_DB_SUBPROCESS_ENABLED": "false",
            "DLT_DATA_DIR": str(root / "dlt"),
            "DLT_CONFIG_FOLDER": str(root / "dlt_config"),
            "DLT_PROJECT_DIR": str(root / "dlt_project"),
            "TMPDIR": str(root / "tmp"),
            "TEMP": str(root / "tmp"),
            "TMP": str(root / "tmp"),
            "TIKTOKEN_CACHE_DIR": str(root / "tokenizers"),
            "ENABLE_BACKEND_ACCESS_CONTROL": "true",
            "REQUIRE_AUTHENTICATION": "true",
            "COGNEE_SKIP_CONNECTION_TEST": "true",
            "COGNEE_SKIP_PREFLIGHT": "1",
            "TELEMETRY_DISABLED": "1",
            "DLTHUB_TELEMETRY": "false",
            "RUNTIME__DLTHUB_TELEMETRY": "false",
            # LiteLLM otherwise fetches its public cost map during import,
            # before model stubs can be installed. Use its bundled copy.
            "LITELLM_LOCAL_MODEL_COST_MAP": "true",
            "COGNEE_TRACING_ENABLED": "false",
            "CACHE_BACKEND": "sqlite",
            "CACHE_DB_URL": "sqlite+aiosqlite:///" + str(root / "cache" / "cache.db"),
            "AUTO_FEEDBACK": "false",
            "IMPROVE_AUTO_ENABLED": "false",
            "SHARED_LADYBUG_LOCK": "false",
            "LLM_PROVIDER": "openai",
            "LLM_MODEL": "openai/gpt-5-mini",
            "LLM_API_KEY": "fixture-unused-model-key",
            "EMBEDDING_PROVIDER": "openai",
            "EMBEDDING_MODEL": "openai/text-embedding-3-small",
            "EMBEDDING_DIMENSIONS": "256",
            "EMBEDDING_API_KEY": "fixture-unused-model-key",
            "KUZU_BUFFER_POOL_SIZE": str(64 * 1024 * 1024),
            # Schema/cleanup operations in the multi-source scenario need more
            # address space; the actual buffer pool stays capped at 64 MiB.
            "KUZU_MAX_DB_SIZE": str(1024 * 1024 * 1024),
            "KUZU_NUM_THREADS": "1",
        }
    )
    return env


def within(root, value):
    """Reject remote, relative, shared and symlink-escaping backend locations."""
    if not isinstance(value, (str, Path)) or not str(value) or "://" in str(value):
        raise AssertionError("Acceptance backend must use a disposable local path")
    path = Path(value)
    if not path.is_absolute() or not path.resolve().is_relative_to(root):
        raise AssertionError("Acceptance backend escaped its disposable root")
    return path


def install_backend_guards(root, patches):
    """Guard actual SQLite, graph, vector and DLT constructors before writes."""
    import dlt
    from cognee.infrastructure.databases.graph.ladybug.adapter import LadybugAdapter
    from cognee.infrastructure.databases.vector.lancedb.LanceDBAdapter import LanceDBAdapter
    from cognee_db_workers import _kuzu_helpers

    if not _kuzu_helpers.bundled_extensions_present():
        raise AssertionError("Offline acceptance requires Cognee's bundled JSON extension")

    def offline_json_extension(execute):
        try:
            execute("LOAD EXTENSION JSON;")
            return
        except Exception as error:
            if "not been installed" not in str(error):
                raise
        bundled = _kuzu_helpers.bundled_json_extension_path(execute)
        if bundled is None:
            raise AssertionError(
                "No compatible bundled JSON extension; remote install is forbidden"
            )
        execute("LOAD EXTENSION '" + bundled.replace("'", "''") + "';")

    patches.enter_context(
        patch.object(_kuzu_helpers, "load_json_extension", offline_json_extension)
    )

    sqlite_connect = sqlite3.connect
    graph_init = LadybugAdapter.__init__
    vector_init = LanceDBAdapter.__init__
    pipeline_factory = dlt.pipeline

    def guarded_sqlite(database, *args, **kwargs):
        if database != ":memory:":
            within(root, database)
        return sqlite_connect(database, *args, **kwargs)

    def guarded_graph(self, db_path, *args, **kwargs):
        within(root, db_path)
        if kwargs.get("subprocess_mode"):
            raise AssertionError("Acceptance must keep graph operations in the guarded process")
        return graph_init(self, db_path, *args, **kwargs)

    def guarded_vector(self, url, *args, **kwargs):
        within(root, url)
        if kwargs.get("connection") is not None or kwargs.get("session") is not None:
            raise AssertionError("Acceptance must keep vector operations in the guarded process")
        return vector_init(self, url, *args, **kwargs)

    def guarded_pipeline(*args, **kwargs):
        kwargs.setdefault("pipelines_dir", str(root / "dlt"))
        within(root, kwargs["pipelines_dir"])
        return pipeline_factory(*args, **kwargs)

    patches.enter_context(patch("sqlite3.connect", guarded_sqlite))
    patches.enter_context(patch("sqlite3.dbapi2.connect", guarded_sqlite))
    patches.enter_context(patch.object(LadybugAdapter, "__init__", guarded_graph))
    patches.enter_context(patch.object(LanceDBAdapter, "__init__", guarded_vector))
    patches.enter_context(patch.object(dlt, "pipeline", guarded_pipeline))


def verify_config(root):
    from cognee.base_config import get_base_config
    from cognee.infrastructure.databases.cache.config import get_cache_config
    from cognee.infrastructure.databases.graph.config import get_graph_config
    from cognee.infrastructure.databases.relational.config import get_relational_config
    from cognee.infrastructure.databases.vector.config import get_vectordb_config

    base = get_base_config()
    for name in ("data", "system", "cache", "logs", "repos"):
        within(root, getattr(base, name + "_root_directory"))
    relational = get_relational_config()
    graph = get_graph_config()
    vector = get_vectordb_config()
    cache = get_cache_config()
    assert cache.cache_backend == "sqlite"
    assert cache.cache_db_url == "sqlite+aiosqlite:///" + str(root / "cache" / "cache.db")
    assert not cache.shared_ladybug_lock
    assert relational.db_provider == "sqlite"
    within(root, relational.db_path)
    assert graph.graph_database_provider == "ladybug"
    assert graph.graph_dataset_database_handler == "ladybug"
    assert not graph.graph_database_subprocess_enabled
    within(root, graph.graph_file_path)
    assert not graph.graph_database_url
    assert vector.vector_db_provider == "lancedb"
    assert vector.vector_dataset_database_handler == "lancedb"
    assert not vector.vector_db_subprocess_enabled
    within(root, vector.vector_db_url)


class MemoryRuntime:
    def __init__(self, cognee, user, root, model_calls):
        self.cognee = cognee
        self.user = user
        self.root = root
        self.model_calls = model_calls

    async def dataset_id(self, name):
        datasets = await self.cognee.datasets.list_datasets(self.user)
        matches = [dataset.id for dataset in datasets if dataset.name == name]
        assert len(matches) == 1, "Expected exactly one acceptance dataset"
        return UUID(str(matches[0]))

    async def documents(self, name, scope=None):
        rows = await self.cognee.datasets.list_data(await self.dataset_id(name), self.user)
        result = {}
        for row in rows:
            metadata = row.system_metadata
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            assert metadata.get("source") == "jenkins"
            if scope is not None and metadata.get("table_name") != scope:
                continue
            external_id = metadata["external_id"]
            assert external_id not in result, "Duplicate external ID in acceptance dataset"
            result[external_id] = str(row.id)
        return result

    async def document_contents(self, name):
        from cognee.infrastructure.files.utils.get_data_file_path import get_data_file_path

        rows = await self.cognee.datasets.list_data(await self.dataset_id(name), self.user)
        contents = {}
        for row in rows:
            metadata = row.system_metadata
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            path = within(self.root, get_data_file_path(row.raw_data_location))
            contents[metadata["external_id"]] = path.read_text(encoding="utf-8")
        return contents

    def source_states(self):
        states = {
            str(path.relative_to(self.root)): json.loads(path.read_text()).get("sources", {})
            for path in (self.root / "dlt").rglob("state.json")
        }
        assert states, "Must inspect actual persisted DLT source state"

        def has_cursor(value):
            if not isinstance(value, dict):
                return False
            jobs = value.get("jobs", {})
            if isinstance(jobs, dict) and jobs.get("api", {}).get("last_build") == 2:
                return True
            return any(has_cursor(child) for child in value.values())

        assert has_cursor(states), "Must inspect the existing Jenkins api cursor at build 2"
        return states

    async def search_marker(self, name, marker):
        from cognee.context_global_variables import set_database_global_context_variables
        from cognee.infrastructure.databases.vector import get_vector_engine_async

        async with set_database_global_context_variables(await self.dataset_id(name), self.user.id):
            engine = await get_vector_engine_async()
            within(self.root, engine.url)
            connection = await engine.get_connection()
            for collection in await connection.table_names():
                table = await connection.open_table(collection)
                for payload in (await table.to_arrow()).column("payload").to_pylist():
                    text = payload.get("text", "")
                    if marker in text:
                        # A real nearest-vector query, using the exact stored
                        # chunk as query so deterministic fixture embeddings
                        # have a meaningful expected nearest neighbour.
                        results = await engine.search(
                            collection, query_text=text, limit=1, include_payload=True
                        )
                        assert results and marker in results[0].payload["text"]
                        return
            raise AssertionError("New build has no searchable text vector")

    async def vector_texts(self, name):
        from cognee.context_global_variables import set_database_global_context_variables
        from cognee.infrastructure.databases.vector import get_vector_engine_async
        from cognee.infrastructure.databases.vector.config import get_vectordb_context_config

        async with set_database_global_context_variables(await self.dataset_id(name), self.user.id):
            config = get_vectordb_context_config()
            assert config["vector_db_provider"] == "lancedb"
            within(self.root, config["vector_db_url"])
            engine = await get_vector_engine_async()
            within(self.root, engine.url)
            connection = await engine.get_connection()
            texts = []
            for collection in await connection.table_names():
                table = await connection.open_table(collection)
                data = await table.to_arrow()
                assert "payload" in data.column_names, "Cannot inspect real vector payloads"
                texts.extend(
                    json.dumps(payload, default=str)
                    for payload in data.column("payload").to_pylist()
                )
            assert texts, "Acceptance must inspect populated real vector collections"
            return texts

    async def graph_names(self, name):
        from cognee.context_global_variables import set_database_global_context_variables
        from cognee.infrastructure.databases.graph import get_graph_engine
        from cognee.infrastructure.databases.graph.config import get_graph_context_config

        async with set_database_global_context_variables(await self.dataset_id(name), self.user.id):
            config = get_graph_context_config()
            assert config["graph_database_provider"] == "ladybug"
            within(self.root, config["graph_file_path"])
            engine = await get_graph_engine()
            nodes, _ = await engine.get_graph_data()
            assert nodes, "Acceptance must inspect populated real graph nodes"
            return [json.dumps(node, default=str) for node in nodes]


async def run_acceptance(root):
    import cognee
    import pytest
    from cognee.modules.engine.operations.setup import setup
    from cognee.modules.users.methods import get_default_user
    from test_remember_acceptance import _stub_paid_models, exercise_memory

    counters = {"network_attempts": 0, "prune_calls": 0}

    def forbidden_prune(*args, **kwargs):
        counters["prune_calls"] += 1
        raise AssertionError("Global prune is prohibited in connector acceptance")

    with ExitStack() as patches, pytest.MonkeyPatch.context() as monkeypatch:
        model_calls = _stub_paid_models(monkeypatch)
        install_backend_guards(root, patches)
        verify_config(root)
        patches.enter_context(patch.object(cognee.prune, "prune_data", forbidden_prune))
        patches.enter_context(patch.object(cognee.prune, "prune_system", forbidden_prune))
        await setup()
        runtime = MemoryRuntime(cognee, await get_default_user(), root, model_calls)
        await exercise_memory(runtime)
    return counters


def main():
    if sys.platform == "win32":
        raise SystemExit("WDAC-blocked Windows execution is not authorized")
    if len(sys.argv) != 2:
        raise SystemExit("Use the opted-in pytest wrapper on an approved runner")
    root = Path(sys.argv[1]).resolve()
    if root.exists():
        raise SystemExit("Acceptance root must not already exist")
    root.mkdir()
    assets = within(root.parent, root.parent / "tokenizer_assets")
    assert assets.is_dir() and any(assets.iterdir()), "Provision tokenizer assets before the worker"
    shutil.copytree(assets, root / "tokenizers")
    for name in (
        "data",
        "system",
        "cache",
        "logs",
        "repos",
        "relational",
        "graph",
        "vector",
        "dlt",
        "dlt_config",
        "dlt_project",
        "tmp",
        "home",
    ):
        (root / name).mkdir()
    expected = sandbox_environment(root)
    assert set(os.environ) <= set(expected) | {"LC_CTYPE"}, "Unexpected inherited environment"
    assert all(os.environ.get(key) == value for key, value in expected.items())
    assert not any(name.startswith("cognee") for name in sys.modules)
    assert version("cognee") == "1.6.5", "Acceptance must use the source-reviewed core pin"
    network_attempts = []
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def deny_network(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            network_attempts.append("".join(traceback.format_stack(limit=16)))
            raise AssertionError("Acceptance permits no network connections")
        return original_connect(sock, address)

    def deny_network_ex(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            network_attempts.append("".join(traceback.format_stack(limit=16)))
            raise AssertionError("Acceptance permits no network connections")
        return original_connect_ex(sock, address)

    # -I omits script paths; these point only at the source under review.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with ExitStack() as patches:
        patches.enter_context(patch("socket.socket.connect", deny_network))
        patches.enter_context(patch("socket.socket.connect_ex", deny_network_ex))
        patches.enter_context(patch("dotenv.load_dotenv", return_value=False))
        patches.enter_context(patch("dotenv.dotenv_values", return_value={}))
        counters = asyncio.run(run_acceptance(root))
    assert not network_attempts, "Caught network attempts invalidate acceptance:\n" + "\n".join(
        network_attempts
    )
    result = {
        "status": "passed",
        "checks": [
            "documents",
            "vectors",
            "graph",
            "source_isolation",
            "dataset_isolation",
            "retained_ids",
            "repeat_sync",
            "document_content",
            "searchability",
            "no_reingestion",
            "failure_memory_retention",
            "failure_cursor_rollback",
            "overlapping_source_ids",
        ],
        "network_attempts": len(network_attempts),
        "prune_calls": counters["prune_calls"],
        "cognee_version": version("cognee"),
    }
    (root / "acceptance_result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
