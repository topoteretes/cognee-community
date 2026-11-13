"""Run real storage E2E in a fresh process with no inherited provider credentials."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.integration
@pytest.mark.e2e
def test_cognee_storage_lifecycle(postgres_fixture, tmp_path):
    writer, reader = postgres_fixture
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
    }
    env.update(
        {
            "USERPROFILE": str(tmp_path),
            "PYTHONIOENCODING": "utf-8",
            "NO_COLOR": "1",
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
            "SYSTEM_ROOT_DIRECTORY": str(tmp_path / "system"),
            "DATA_ROOT_DIRECTORY": str(tmp_path / "data"),
            "CACHE_ROOT_DIRECTORY": str(tmp_path / "cache"),
            "COGNEE_LOGS_DIR": str(tmp_path / "logs"),
            "DLT_DATA_DIR": str(tmp_path / "dlt"),
            "DLT_TELEMETRY": "false",
            "DB_PROVIDER": "sqlite",
            "GRAPH_DATABASE_PROVIDER": "ladybug",
            "VECTOR_DB_PROVIDER": "lancedb",
            "ENABLE_BACKEND_ACCESS_CONTROL": "true",
            "COGNEE_SKIP_CONNECTION_TEST": "true",
            "LLM_API_KEY": "synthetic-unused-test-key",
            "LLM_PROVIDER": "openai",
            "EMBEDDING_PROVIDER": "openai",
            "EMBEDDING_MODEL": "text-embedding-3-small",
            "EMBEDDING_DIMENSIONS": "8",
            "IMPROVE_AUTO_ENABLED": "false",
            "COGNEE_TRACING_ENABLED": "false",
            "PG_TEST_WRITER": writer.url.render_as_string(hide_password=False),
            "PG_TEST_READER": reader.url.render_as_string(hide_password=False),
        }
    )
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("cognee_e2e_worker.py"))],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=360,
    )
    (tmp_path / "e2e.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    assert result.returncode == 0, (result.stdout + result.stderr)[-14000:]
    assert "COGNEE_STORAGE_E2E_OK" in result.stdout
    print(next(line for line in result.stdout.splitlines() if "COGNEE_STORAGE_E2E_OK" in line))
