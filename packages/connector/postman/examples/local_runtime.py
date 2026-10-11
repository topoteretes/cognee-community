"""Dedicated local SQLite/Ladybug/LanceDB configuration for the examples."""

import os
from pathlib import Path


def configure_runtime(runtime: Path | None = None) -> Path:
    runtime = (
        (
            runtime
            or Path(
                os.getenv(
                    "POSTMAN_RUNTIME_DIR", str(Path.home() / ".local/share/cognee-postman-4711")
                )
            )
        )
        .expanduser()
        .resolve()
    )
    os.umask(0o077)
    runtime.mkdir(parents=True, exist_ok=True)
    # Backend connection overrides must not redirect this demo to existing stores.
    for key in list(os.environ):
        if key.startswith(("DB_", "MIGRATION_DB_", "GRAPH_DATABASE_", "VECTOR_DB_")) or key in {
            "GRAPH_FILE_PATH",
            "GRAPH_FILENAME",
            "GRAPH_DATASET_DATABASE_HANDLER",
            "VECTOR_DATASET_DATABASE_HANDLER",
            "DATABASE_CONNECT_ARGS",
            "POOL_ARGS",
            "VECTOR_POOL_ARGS",
            "LOG_FILE_NAME",
        }:
            os.environ.pop(key)
    os.environ.update(
        {
            "SYSTEM_ROOT_DIRECTORY": str(runtime / "system"),
            "DATA_ROOT_DIRECTORY": str(runtime / "data"),
            "CACHE_ROOT_DIRECTORY": str(runtime / "cache"),
            "LOGS_ROOT_DIRECTORY": str(runtime / "logs"),
            "COGNEE_LOGS_DIR": str(runtime / "logs"),
            "COGNEE_REPOS_DIR": str(runtime / "repos"),
            "DB_PROVIDER": "sqlite",
            "DB_PATH": str(runtime / "system/databases"),
            "DB_NAME": "cognee_db",
            "GRAPH_DATABASE_PROVIDER": "ladybug",
            "GRAPH_DATASET_DATABASE_HANDLER": "ladybug",
            "VECTOR_DB_PROVIDER": "lancedb",
            "VECTOR_DATASET_DATABASE_HANDLER": "lancedb",
            "STORAGE_BACKEND": "local",
            "CACHE_BACKEND": "sqlite",
            "CACHE_DB_URL": "sqlite+aiosqlite:///" + str(runtime / "cache.db"),
            "ENABLE_BACKEND_ACCESS_CONTROL": "true",
            "RUNTIME__DLTHUB_TELEMETRY": "false",
        }
    )
    os.chdir(runtime)
    return runtime
