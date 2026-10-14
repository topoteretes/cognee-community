"""Offline acceptance through public sync with real staging, graph, and vectors.

Only Read.ai/OIDC HTTP and LLM/embedding boundaries are replaced. Storage,
indexing, cognification, provenance, retrieval, and deletion remain production code.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import cognee
import pytest
import pytest_asyncio
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.infrastructure.databases.vector import get_vector_engine_async
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.search.types import SearchType
from cognee.modules.users.methods import get_default_user

from cognee_community_connector_read_ai import compat, read_ai_source, sync_read_ai
from cognee_community_connector_read_ai.models import ReadAIError

EXAMPLE_PATH = Path(__file__).parents[1] / "examples" / "offline.py"
spec = importlib.util.spec_from_file_location("read_ai_offline", EXAMPLE_PATH)
offline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(offline)

DATASET = "read_ai_acceptance"


@pytest_asyncio.fixture
async def storage(tmp_path):
    offline.configure_storage(tmp_path)
    models = offline.SyntheticModels().install()
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    try:
        yield models
    finally:
        await cognee.prune.prune_data()
        await cognee.prune.prune_system(metadata=True)
        models.close()


def source(provider, root, connection="primary", **kwargs):
    return read_ai_source(
        connection_id=connection,
        token_provider=provider,
        client=provider.client,
        state_dir=root / "connector",
        **kwargs,
    )


async def records(dataset=DATASET):
    user = await get_default_user()
    datasets = await get_authorized_existing_datasets(
        user=user, permission_type="write", datasets=[dataset]
    )
    if not datasets:
        return {}
    items = await get_dataset_data(datasets[0].id)
    return {
        item.system_metadata["external_id"]: item
        for item in items
        if isinstance(item.system_metadata, dict)
        and item.system_metadata.get("source") == "read_ai"
    }


async def graph_has(term):
    nodes, _ = await (await get_graph_engine()).get_graph_data()
    return any(term.lower() in str(props).lower() for _, props in nodes)


async def chunks():
    nodes, _ = await (await get_graph_engine()).get_graph_data()
    return {
        str(identity): props
        for identity, props in nodes
        if props.get("type") == "DocumentChunk" or "text" in props and "chunk_index" in props
    }


async def retrieve(term, dataset=DATASET):
    result = await cognee.search(term, query_type=SearchType.CHUNKS, datasets=[dataset], top_k=20)
    return str(result)


@pytest.mark.asyncio
async def test_real_lifecycle_edit_removal_and_final_delete(storage, tmp_path):
    one = offline.meeting(1, f"{offline.ALPHA} and {offline.SHARED} approved logistics.")
    two = offline.meeting(2, f"{offline.BRAVO} and {offline.SHARED} approved finance.")
    provider = offline.SyntheticReadAI([one, two])
    try:
        initial = await sync_read_ai(source(provider, tmp_path), dataset_name=DATASET)
        first = await records()
        assert initial.created == 2 and len(first) == 2
        first_ids = {key: item.id for key, item in first.items()}
        assert await graph_has(offline.ALPHA) and await graph_has(offline.BRAVO)
        assert offline.ALPHA in await retrieve(offline.ALPHA)
        assert offline.BRAVO in await retrieve(offline.BRAVO)
        graph_context = await cognee.search(
            offline.ALPHA,
            query_type=SearchType.GRAPH_COMPLETION,
            datasets=[DATASET],
            node_type=None,
            only_context=True,
            top_k=20,
        )
        assert offline.ALPHA in str(graph_context) and "works_with" in str(graph_context)
        assert "private@example.invalid" not in await retrieve(offline.ALPHA)
        assert "engagement: 0" in await retrieve(offline.ALPHA)

        old_chunks = await chunks()
        assert old_chunks, "real cognification must produce document chunks"
        engine = await get_vector_engine_async()
        assert await engine.retrieve("DocumentChunk_text", list(old_chunks)), (
            "actual LanceDB chunk rows must exist; indexing may not be mocked away"
        )
        previous_calls = (storage.extractions, storage.embeddings)
        unchanged = await sync_read_ai(source(provider, tmp_path), dataset_name=DATASET)
        assert unchanged.unchanged == 2
        assert {key: item.id for key, item in (await records()).items()} == first_ids
        assert (storage.extractions, storage.embeddings) == previous_calls

        provider.meetings[one["id"]] = offline.meeting(
            1, f"{offline.CHARLIE} and {offline.SHARED} approved new logistics."
        )
        edited = await sync_read_ai(source(provider, tmp_path), dataset_name=DATASET)
        current = await records()
        assert edited.changed == 1 and edited.unchanged == 1
        assert current[one["id"]].id != first_ids[one["id"]]
        assert current[two["id"]].id == first_ids[two["id"]]
        assert not await graph_has(offline.ALPHA)
        assert await graph_has(offline.CHARLIE)
        assert offline.ALPHA not in await retrieve(offline.ALPHA)
        removed_chunks = set(old_chunks) - set(await chunks())
        assert removed_chunks and not await engine.retrieve(
            "DocumentChunk_text", list(removed_chunks)
        )

        del provider.meetings[two["id"]]
        deleted = await sync_read_ai(source(provider, tmp_path), dataset_name=DATASET)
        assert deleted.forgotten == 1
        assert set(await records()) == {one["id"]}
        assert not await graph_has(offline.BRAVO)
        assert await graph_has(offline.SHARED), "shared entity still supported by surviving meeting"
        assert offline.BRAVO not in await retrieve(offline.BRAVO)

        provider.meetings.clear()
        final = await sync_read_ai(source(provider, tmp_path), dataset_name=DATASET)
        assert final.forgotten == 1 and not await records()
        assert not await graph_has(offline.CHARLIE)
        assert not await graph_has(offline.SHARED)
        assert not await chunks()
    finally:
        provider.client.close()


@pytest.mark.asyncio
async def test_section_withdrawal_rehydration_and_nested_metrics(storage, tmp_path):
    raw = offline.meeting(1, f"{offline.ALPHA} approved the project.")
    raw["metrics"] = {"engagement": 0, "scores": {"sentiment": 0.9, "clarity": 0.5}}
    provider = offline.SyntheticReadAI([raw])
    try:
        await sync_read_ai(source(provider, tmp_path), dataset_name=DATASET)
        raw["metrics"] = {"engagement": 0, "scores": {"sentiment": None}}
        await sync_read_ai(source(provider, tmp_path), dataset_name=DATASET)
        text = await retrieve(offline.ALPHA)
        assert "engagement: 0" in text and "scores.clarity" not in text
        assert "scores.sentiment" not in text
        raw.update(summary=None, transcript=None, metrics=None)
        withdrawn = await sync_read_ai(source(provider, tmp_path), dataset_name=DATASET)
        assert withdrawn.withdrawn == 1 and withdrawn.pending == 1
        assert not await records() and not await graph_has(offline.ALPHA)
        raw["summary"] = f"{offline.BRAVO} restored the meeting summary."
        restored = await sync_read_ai(source(provider, tmp_path), dataset_name=DATASET)
        assert restored.created == 1 and len(await records()) == 1
        assert await graph_has(offline.BRAVO)
    finally:
        provider.client.close()


@pytest.mark.asyncio
async def test_isolation_other_connections_datasets_and_unrelated_data(storage, tmp_path):
    one = offline.meeting(1, f"{offline.ALPHA} and {offline.SHARED} approved logistics.")
    two = offline.meeting(2, f"{offline.BRAVO} and {offline.SHARED} approved finance.")
    primary = offline.SyntheticReadAI([one])
    other = offline.SyntheticReadAI([two])
    try:
        await sync_read_ai(source(primary, tmp_path), dataset_name=DATASET)
        await sync_read_ai(source(other, tmp_path, "other"), dataset_name=DATASET)
        await sync_read_ai(source(other, tmp_path, "other"), dataset_name="other_dataset")
        await cognee.add("Unrelated baseline document", dataset_name=DATASET)
        user = await get_default_user()
        dataset = (
            await get_authorized_existing_datasets(
                user=user, permission_type="write", datasets=[DATASET]
            )
        )[0]
        original_all = await get_dataset_data(dataset.id)
        unrelated = [item.id for item in original_all if not item.system_metadata]
        retained = (await records())[two["id"]].id
        primary.meetings.clear()
        await sync_read_ai(source(primary, tmp_path), dataset_name=DATASET)
        assert set(await records()) == {two["id"]}
        assert (await records())[two["id"]].id == retained
        assert len(await records("other_dataset")) == 1
        assert await graph_has(offline.BRAVO) and await graph_has(offline.SHARED)
        assert set(unrelated) <= {item.id for item in await get_dataset_data(dataset.id)}
    finally:
        primary.client.close()
        other.client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["cognify", "verify_corpus"])
async def test_unchanged_recovery_revalidates_before_replay(storage, tmp_path, monkeypatch, phase):
    one = offline.meeting(1, f"{offline.ALPHA} approved the project.")
    provider = offline.SyntheticReadAI([one])
    original = getattr(compat, phase)

    async def fail(*args, **kwargs):
        raise RuntimeError("synthetic interrupted phase")

    try:
        monkeypatch.setattr(compat, phase, fail)
        with pytest.raises((ReadAIError, RuntimeError)):
            await sync_read_ai(source(provider, tmp_path), dataset_name=DATASET)
        monkeypatch.setattr(compat, phase, original)
        provider.denied.add(one["id"])
        with pytest.raises(ReadAIError):
            await sync_read_ai(source(provider, tmp_path), dataset_name=DATASET)
        provider.denied.clear()
        result = await sync_read_ai(source(provider, tmp_path), dataset_name=DATASET)
        assert result.reconciled and len(await records()) == 1
        assert await graph_has(offline.ALPHA)
    finally:
        provider.client.close()


@pytest.mark.asyncio
async def test_merge_options_reach_actual_add_entrypoint(storage, tmp_path, monkeypatch):
    provider = offline.SyntheticReadAI([offline.meeting(1, f"{offline.ALPHA} approved.")])
    original = cognee.add
    calls = []

    async def observe(*args, **kwargs):
        calls.append(kwargs.copy())
        return await original(*args, **kwargs)

    monkeypatch.setattr(cognee, "add", observe)
    try:
        await sync_read_ai(source(provider, tmp_path), dataset_name=DATASET)
        assert calls and calls[0]["dlt_config"]["write_disposition"] == "merge"
        assert calls[0]["dlt_config"]["primary_key"] == "id"
        assert calls[0].get("run_in_background", False) is False
    finally:
        provider.client.close()


def worker_command(root, meetings, *flags):
    import sys

    return [
        sys.executable,
        str(Path(__file__).with_name("process_worker.py")),
        str(root),
        str(meetings),
        *flags,
    ]


def worker_result(process):
    import json

    marker = "WORKER_RESULT="
    return json.loads(
        next(line[len(marker) :] for line in process.stdout.splitlines() if line.startswith(marker))
    )


@pytest.mark.parametrize("phase", ["add_rows", "cognify", "verify_corpus"])
def test_new_process_restart_recovers_unchanged_upstream(tmp_path, phase):
    import json
    import os
    import subprocess

    raw = offline.meeting(1, f"{offline.ALPHA} and {offline.SHARED} approved the project.")
    fixture = tmp_path / "upstream.json"
    fixture.write_text(json.dumps([raw]))
    env = {**os.environ, "COGNEE_LOG_FILE": "false"}
    interrupted = subprocess.run(
        worker_command(tmp_path, fixture, "--crash", phase),
        capture_output=True,
        text=True,
        env=env,
        timeout=90,
    )
    assert interrupted.returncode == 77, interrupted.stdout + interrupted.stderr
    recovered = subprocess.run(
        worker_command(tmp_path, fixture), capture_output=True, text=True, env=env, timeout=90
    )
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    result = worker_result(recovered)
    assert set(result["records"]) == {raw["id"]}
    third = subprocess.run(
        worker_command(tmp_path, fixture), capture_output=True, text=True, env=env, timeout=90
    )
    assert third.returncode == 0, third.stdout + third.stderr
    stable = worker_result(third)
    assert stable["records"] == result["records"]
    assert stable["extractions"] == 0 and stable["embeddings"] == 0


def test_deleted_upstream_before_process_recovery_is_not_replayed(tmp_path):
    import json
    import os
    import subprocess

    fixture = tmp_path / "upstream.json"
    fixture.write_text(json.dumps([offline.meeting(1, f"{offline.ALPHA} approved.")]))
    env = {**os.environ, "COGNEE_LOG_FILE": "false"}
    interrupted = subprocess.run(
        worker_command(tmp_path, fixture, "--crash", "add_rows"),
        capture_output=True,
        text=True,
        env=env,
        timeout=90,
    )
    assert interrupted.returncode == 77, interrupted.stdout + interrupted.stderr
    fixture.write_text("[]")
    recovered = subprocess.run(
        worker_command(tmp_path, fixture), capture_output=True, text=True, env=env, timeout=90
    )
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert worker_result(recovered)["records"] == {}


def test_two_processes_serialize_same_connection_without_duplicates(tmp_path):
    import json
    import os
    import subprocess

    raw = offline.meeting(1, f"{offline.ALPHA} approved the concurrent project.")
    fixture = tmp_path / "upstream.json"
    fixture.write_text(json.dumps([raw]))
    env = {**os.environ, "COGNEE_LOG_FILE": "false"}
    writers = [
        subprocess.Popen(
            worker_command(tmp_path, fixture),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for _ in range(2)
    ]
    results = []
    try:
        for writer in writers:
            stdout, stderr = writer.communicate(timeout=90)
            assert writer.returncode == 0, stdout + stderr
            from types import SimpleNamespace

            results.append(worker_result(SimpleNamespace(stdout=stdout)))
    finally:
        for writer in writers:
            if writer.poll() is None:
                writer.kill()
                writer.communicate()
    assert len(results[0]["records"]) == 1
    assert results[0]["records"] == results[1]["records"]
    assert sorted(result["extractions"] for result in results) == [0, 1]


@pytest.mark.asyncio
async def test_selected_sections_and_connection_retirement(storage, tmp_path):
    from cognee_community_connector_read_ai import ConfigurationError, retire_read_ai

    raw = offline.meeting(1, f"{offline.ALPHA} is approved in the summary.")
    raw["transcript"]["turns"][0]["text"] = f"{offline.BRAVO} is only in the transcript."
    provider = offline.SyntheticReadAI([raw])
    selection = {"include_transcript": False, "include_metrics": False}
    try:
        result = await sync_read_ai(source(provider, tmp_path, **selection), dataset_name=DATASET)
        assert result.created == 1
        text = await retrieve(offline.ALPHA)
        assert offline.ALPHA in text and offline.BRAVO not in text
        assert "engagement" not in text and "## Transcript" not in text
        assert await graph_has(offline.ALPHA) and not await graph_has(offline.BRAVO)
        with pytest.raises(ConfigurationError):
            await sync_read_ai(source(provider, tmp_path), dataset_name=DATASET)
        await retire_read_ai(source(provider, tmp_path, **selection), dataset_name=DATASET)
        assert not await records() and not await graph_has(offline.ALPHA)
        with pytest.raises(ConfigurationError):
            await sync_read_ai(source(provider, tmp_path, **selection), dataset_name=DATASET)
    finally:
        provider.client.close()


def test_permission_loss_before_new_process_recovery_does_not_acknowledge(tmp_path):
    import json
    import os
    import subprocess

    fixture = tmp_path / "upstream.json"
    fixture.write_text(json.dumps([offline.meeting(1, f"{offline.ALPHA} approved.")]))
    env = {**os.environ, "COGNEE_LOG_FILE": "false"}
    interrupted = subprocess.run(
        worker_command(tmp_path, fixture, "--crash", "add_rows"),
        capture_output=True,
        text=True,
        env=env,
        timeout=90,
    )
    assert interrupted.returncode == 77, interrupted.stdout + interrupted.stderr
    journals = [
        path
        for path in (tmp_path / "connector").rglob("*.json")
        if isinstance((value := json.loads(path.read_text())), dict)
        and value.get("version") == 1
        and "last_success" in value
    ]
    assert len(journals) == 1
    checkpoint = json.loads(journals[0].read_text())["last_success"]
    denied = subprocess.run(
        worker_command(tmp_path, fixture, "--denied"),
        capture_output=True,
        text=True,
        env=env,
        timeout=90,
    )
    assert denied.returncode != 0
    assert json.loads(journals[0].read_text())["last_success"] == checkpoint
    recovered = subprocess.run(
        worker_command(tmp_path, fixture), capture_output=True, text=True, env=env, timeout=90
    )
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert len(worker_result(recovered)["records"]) == 1


def test_new_process_restart_after_cleanup_preserves_surviving_evidence(tmp_path):
    import json
    import os
    import subprocess

    one = offline.meeting(1, f"{offline.ALPHA} and {offline.SHARED} approved logistics.")
    two = offline.meeting(2, f"{offline.BRAVO} and {offline.SHARED} approved finance.")
    fixture = tmp_path / "upstream.json"
    fixture.write_text(json.dumps([one, two]))
    env = {**os.environ, "COGNEE_LOG_FILE": "false"}
    initial = subprocess.run(
        worker_command(tmp_path, fixture), capture_output=True, text=True, env=env, timeout=90
    )
    assert initial.returncode == 0, initial.stdout + initial.stderr
    original = worker_result(initial)["records"]
    fixture.write_text(json.dumps([one]))
    interrupted = subprocess.run(
        worker_command(tmp_path, fixture, "--crash", "verify_corpus"),
        capture_output=True,
        text=True,
        env=env,
        timeout=90,
    )
    assert interrupted.returncode == 77, interrupted.stdout + interrupted.stderr
    recovered = subprocess.run(
        worker_command(tmp_path, fixture), capture_output=True, text=True, env=env, timeout=90
    )
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    result = worker_result(recovered)
    assert result["records"] == {one["id"]: original[one["id"]]}
    assert result["extractions"] == 0 and result["embeddings"] == 0


@pytest.mark.asyncio
async def test_default_access_control_private_store_edit_and_delete(storage, tmp_path, monkeypatch):
    from cognee.context_global_variables import set_database_global_context_variables

    monkeypatch.setenv("ENABLE_BACKEND_ACCESS_CONTROL", "true")
    one = offline.meeting(1, f"{offline.ALPHA} and {offline.SHARED} approved.")
    provider = offline.SyntheticReadAI([one])
    try:
        initial = await sync_read_ai(source(provider, tmp_path), dataset_name=DATASET)
        user = await get_default_user()
        dataset = (
            await get_authorized_existing_datasets(
                user=user, permission_type="write", datasets=[DATASET]
            )
        )[0]
        table = (await get_dataset_data(dataset.id))[0].system_metadata["table_name"].split(".")[-1]
        assert initial.created == 1 and len(await compat.scoped_data(dataset, table)) == 1
        async with set_database_global_context_variables(dataset.id, dataset.owner_id):
            assert await graph_has(offline.ALPHA)
            assert offline.ALPHA in await retrieve(offline.ALPHA)
        provider.meetings[one["id"]] = offline.meeting(1, f"{offline.BRAVO} approved.")
        edited = await sync_read_ai(source(provider, tmp_path), dataset_name=DATASET)
        assert edited.changed == 1
        async with set_database_global_context_variables(dataset.id, dataset.owner_id):
            assert not await graph_has(offline.ALPHA) and await graph_has(offline.BRAVO)
        provider.meetings.clear()
        removed = await sync_read_ai(source(provider, tmp_path), dataset_name=DATASET)
        assert removed.forgotten == 1 and not await compat.scoped_data(dataset, table)
        async with set_database_global_context_variables(dataset.id, dataset.owner_id):
            assert not await graph_has(offline.BRAVO) and not await chunks()
    finally:
        provider.client.close()


@pytest.mark.asyncio
async def test_real_pending_load_rebuilds_partially_modified_owned_table(
    storage, tmp_path, monkeypatch
):
    import importlib
    import json

    from cognee.tasks.ingestion.exceptions.exceptions import DLTIngestionError

    one = offline.meeting(1, f"{offline.ALPHA} and {offline.SHARED} approved.")
    two = offline.meeting(2, f"{offline.BRAVO} and {offline.SHARED} approved.")
    primary = offline.SyntheticReadAI([one])
    other = offline.SyntheticReadAI([two])
    try:
        await sync_read_ai(source(primary, tmp_path), dataset_name=DATASET)
        await sync_read_ai(source(other, tmp_path, "other"), dataset_name=DATASET)
        baseline = await records()
        retained_id = baseline[two["id"]].id
        owned_table = baseline[one["id"]].system_metadata["table_name"].split(".")[-1]
        journals = [
            path
            for path in (tmp_path / "connector" / "connections").glob("*.json")
            if json.loads(path.read_text())["configuration"]["connection_id"] == "primary"
        ]
        assert len(journals) == 1
        journal = journals[0]
        checkpoint = json.loads(journal.read_text())["last_success"]
        primary.meetings[one["id"]] = offline.meeting(1, f"{offline.CHARLIE} approved.")
        pipeline_class = importlib.import_module("dlt.pipeline.pipeline").Pipeline
        original_load = pipeline_class.load
        failed_pipelines = []

        def interrupt_load(pipeline, *args, **kwargs):
            if owned_table in pipeline.default_schema.tables and pipeline.has_pending_data:
                with pipeline.sql_client() as sql:
                    sql.execute_sql(f"DELETE FROM {sql.make_qualified_table_name(owned_table)}")
                failed_pipelines.append(pipeline)
                raise RuntimeError("synthetic partial destination failure")
            return original_load(pipeline, *args, **kwargs)

        monkeypatch.setattr(pipeline_class, "load", interrupt_load)
        with pytest.raises((ReadAIError, DLTIngestionError, RuntimeError)):
            await sync_read_ai(source(primary, tmp_path), dataset_name=DATASET)
        assert len(failed_pipelines) == 1 and failed_pipelines[0].has_pending_data
        assert json.loads(journal.read_text())["last_success"] == checkpoint
        assert json.loads(journal.read_text())["attempt"]["phase"] == "loading"
        monkeypatch.setattr(pipeline_class, "load", original_load)
        original_add_rows = compat.add_rows

        async def interrupt_rebuild(bound, dataset_name, user, write_disposition="merge"):
            assert write_disposition == "replace"
            monkeypatch.setattr(pipeline_class, "load", interrupt_load)
            try:
                return await original_add_rows(bound, dataset_name, user, write_disposition)
            finally:
                monkeypatch.setattr(pipeline_class, "load", original_load)

        monkeypatch.setattr(compat, "add_rows", interrupt_rebuild)
        with pytest.raises((ReadAIError, DLTIngestionError, RuntimeError)):
            await sync_read_ai(source(primary, tmp_path), dataset_name=DATASET)
        assert len(failed_pipelines) == 2
        unfinished = json.loads(journal.read_text())
        assert unfinished["last_success"] == checkpoint
        assert unfinished["attempt"]["rebuild_required"] is True
        monkeypatch.setattr(compat, "add_rows", original_add_rows)
        recovered = await sync_read_ai(source(primary, tmp_path), dataset_name=DATASET)
        assert recovered.changed == 1
        current = await records()
        assert current[two["id"]].id == retained_id
        assert current[one["id"]].id != baseline[one["id"]].id
        assert await graph_has(offline.CHARLIE) and not await graph_has(offline.ALPHA)
        assert await graph_has(offline.BRAVO)
        assert not failed_pipelines[0].has_pending_data
        assert json.loads(journal.read_text())["attempt"] is None
    finally:
        primary.client.close()
        other.client.close()


@pytest.mark.asyncio
async def test_explicit_selection_and_default_user_alias_share_checkpoint(storage, tmp_path):
    from cognee_community_connector_read_ai import ConfigurationError

    one = offline.meeting(1, f"{offline.ALPHA} approved.")
    two = offline.meeting(2, f"{offline.BRAVO} is outside the explicit selection.")
    provider = offline.SyntheticReadAI([one, two])
    options = {"meeting_ids": [one["id"]]}
    try:
        await sync_read_ai(source(provider, tmp_path, **options), dataset_name=DATASET)
        first = await records()
        assert set(first) == {one["id"]}
        repeated = await sync_read_ai(source(provider, tmp_path, **options), dataset_name=DATASET)
        assert repeated.unchanged == 1
        assert (await records())[one["id"]].id == first[one["id"]].id
        user = await get_default_user()
        explicit_user = await sync_read_ai(
            source(provider, tmp_path, **options), dataset_name=DATASET, user=user
        )
        assert explicit_user.unchanged == 1 and explicit_user.created == 0
        with pytest.raises(ConfigurationError):
            await sync_read_ai(
                source(provider, tmp_path, **options, include_transcript=False),
                dataset_name=DATASET,
                user=user,
            )
    finally:
        provider.client.close()


@pytest.mark.asyncio
async def test_upstream_failures_and_every_budget_preserve_real_import(storage, tmp_path):
    """A previously searchable meeting must survive every incomplete extraction."""
    import json

    import httpx

    from cognee_community_connector_read_ai import BudgetExceededError, SyncLimits

    raw = offline.meeting(1, f"{offline.ALPHA} approved the recovery test.")
    extra = offline.meeting(2, "Additional inventory entry")
    provider = offline.SyntheticReadAI([raw])
    original_client = provider.client
    try:
        await sync_read_ai(source(provider, tmp_path), dataset_name=DATASET)
        journal = next((tmp_path / "connector" / "connections").glob("*.json"))
        successful_state = json.loads(journal.read_text())
        document_ids = {key: str(item.id) for key, item in (await records()).items()}
        chunk_ids = set(await chunks())
        model_calls = (storage.extractions, storage.embeddings)
        jwks_bytes = len(httpx.Response(200, json={"keys": [provider.jwk]}).content)
        userinfo_bytes = len(httpx.Response(200, json={"sub": provider.subject}).content)
        cases = [
            ("malformed_page", SyncLimits(), ReadAIError),
            ("partial_pagination", SyncLimits(), ReadAIError),
            ("exhausted_retries", SyncLimits(), ReadAIError),
            ("permission_loss", SyncLimits(), ReadAIError),
            ("malformed_section", SyncLimits(), ReadAIError),
            ("meetings", SyncLimits(max_meetings=1), BudgetExceededError),
            ("pages", SyncLimits(max_pages=1), BudgetExceededError),
            ("response", SyncLimits(max_response_bytes=1), BudgetExceededError),
            (
                "total_response",
                SyncLimits(max_total_response_bytes=jwks_bytes + userinfo_bytes - 1),
                BudgetExceededError,
            ),
            ("deadline", SyncLimits(max_duration_seconds=1e-9), BudgetExceededError),
        ]
        for scenario, limits, error_type in cases:

            def respond(request, *, scenario=scenario):
                if request.url.path == "/v1/meetings":
                    if scenario == "exhausted_retries":
                        return httpx.Response(503, headers={"Retry-After": "0"})
                    if scenario == "malformed_page" or (
                        scenario == "partial_pagination" and request.url.params.get("cursor")
                    ):
                        return httpx.Response(200, json={"object": "list", "data": "invalid"})
                    if scenario in ("partial_pagination", "pages", "meetings"):
                        return httpx.Response(
                            200,
                            json={
                                "object": "list",
                                # Previously imported raw is absent here. A partial
                                # page must never authorize its removal.
                                "data": [extra],
                                "has_more": scenario != "meetings",
                            },
                        )
                if request.url.path == f"/v1/meetings/{raw['id']}":
                    if scenario == "permission_loss":
                        return httpx.Response(403)
                    if scenario == "malformed_section":
                        return httpx.Response(200, json={**raw, "summary": {"invalid": True}})
                return provider.respond(request)

            with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
                provider.client = transport
                with pytest.raises(error_type):
                    await sync_read_ai(
                        source(provider, tmp_path, limits=limits), dataset_name=DATASET
                    )
            assert json.loads(journal.read_text()) == successful_state, scenario
            assert {key: str(item.id) for key, item in (await records()).items()} == document_ids, (
                scenario
            )
            assert set(await chunks()) == chunk_ids, scenario
            vectors = await (await get_vector_engine_async()).retrieve(
                "DocumentChunk_text", list(chunk_ids)
            )
            assert len(vectors) == len(chunk_ids), scenario
            assert (storage.extractions, storage.embeddings) == model_calls, scenario
        assert offline.ALPHA in await retrieve(offline.ALPHA)
    finally:
        provider.client = original_client
        original_client.close()
