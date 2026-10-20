"""Real Cognee ingestion in isolated subprocesses; optional model-backed search.

The offline worker uses actual add(), document expansion, SQLite staging, Data
records and orphan cleanup. The live worker additionally uses actual remember(),
cognify() and search(), with synthetic LogRocket responses and configured models.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest


def run_worker(tmp_path, mode):
    env = dict(os.environ)
    env.update(
        {
            "SYSTEM_ROOT_DIRECTORY": str(tmp_path / "system"),
            "DATA_ROOT_DIRECTORY": str(tmp_path / "data"),
            "CACHE_ROOT_DIRECTORY": str(tmp_path / "cache"),
            "DLT_DATA_DIR": str(tmp_path / "dlt"),
            "COGNEE_LOGS_DIR": str(tmp_path / "logs"),
            "DB_PROVIDER": "sqlite",
            "DB_PATH": str(tmp_path / "databases"),
            "DB_NAME": "acceptance",
            "CACHING": "false",
            "ENABLE_BACKEND_ACCESS_CONTROL": "false",
            "TELEMETRY_DISABLED": "true",
        }
    )
    env.pop("LOG_FILE_NAME", None)
    # No .env is loaded from the package: the subprocess runs in a temporary cwd.
    if mode == "offline":
        env["COGNEE_SKIP_CONNECTION_TEST"] = "true"
        for key in tuple(env):
            if "API_KEY" in key or key.startswith("LOGROCKET_"):
                env.pop(key)
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--worker", mode],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=int(env.get("LOGROCKET_COGNEE_E2E_TIMEOUT", "300")) if mode == "live" else 90,
    )
    assert proc.returncode == 0, f"Cognee {mode} acceptance worker failed. " + (
        proc.stdout[-5000:] + proc.stderr[-5000:]
        if mode == "offline"
        else "Model-backed validation failed; inspect provider configuration locally."
    )
    assert "COGNEE_ACCEPTANCE_OK" in proc.stdout


def test_real_cognee_add_retains_missing_documents_and_replaces_edits(tmp_path):
    run_worker(tmp_path, "offline")


@pytest.mark.skipif(
    os.environ.get("LOGROCKET_RUN_COGNEE_E2E") != "1",
    reason="Set LOGROCKET_RUN_COGNEE_E2E=1 with LLM/embedding configuration",
)
def test_real_cognee_remember_and_search(tmp_path):
    run_worker(tmp_path, "live")


async def acceptance_worker(mode):
    import cognee
    from cognee.infrastructure.databases.relational import get_relational_engine
    from cognee.modules.data.models import Data
    from sqlalchemy import select
    from test_logrocket import FakeClient, issue, session

    from cognee_community_connector_logrocket import logrocket_source, remember_logrocket

    dataset_name = "logrocket_acceptance"

    def source(session_records, issue_records):
        return logrocket_source(
            "fixture",
            "project",
            limit=5,
            acknowledge_bounded_results=True,
            client=FakeClient([{"sessionDefinitions": session_records}, {"issues": issue_records}]),
        )

    async def ingest(sessions, issues):
        data = source(sessions, issues)
        if mode == "live":
            result = await remember_logrocket(
                data, dataset_name=dataset_name, self_improvement=False
            )
            assert result.status == "completed", f"Cognee remember status: {result.status}"
        else:
            await cognee.add(
                data, dataset_name=dataset_name, write_disposition="merge", primary_key="id"
            )

    async def stored():
        async with get_relational_engine().get_async_session() as db_session:
            return list((await db_session.execute(select(Data))).scalars().all())

    marker = "CheckoutObservatory"
    report = issue(
        messages=[
            {
                "title": f"{marker} payment failure",
                "message": "Payment failed after checkout submission",
            }
        ]
    )
    await ingest([session(durationMs=1000)], [report])
    first = await stored()
    assert len(first) == 2
    first_ids = {row.id for row in first}
    assert {row.external_metadata["source"] for row in first} == {"logrocket"}

    await ingest([session(durationMs=1000)], [report])
    assert {row.id for row in await stored()} == first_ids

    # A search omitting the issue must retain it. An edited session gets new
    # content while normal Cognee cleanup removes the previous content version.
    await ingest([session(durationMs=2000)], [])
    updated = await stored()
    assert len(updated) == 2
    assert len(first_ids & {row.id for row in updated}) == 1
    assert any(row.external_metadata["title"] == f"{marker} payment failure" for row in updated)

    await ingest([], [])
    assert {row.id for row in await stored()} == {row.id for row in updated}

    if mode == "live":
        results = await cognee.search(
            query_text=f"{marker} payment failure",
            query_type=cognee.SearchType.CHUNKS,
            datasets=[dataset_name],
        )
        assert results, "Search returned no results for the ingested fixture"
        assert marker in str(results), "Search did not return the ingested fixture text"

    print("COGNEE_ACCEPTANCE_OK")


if __name__ == "__main__":
    import asyncio

    asyncio.run(acceptance_worker(sys.argv[2]))
