"""Lifecycle unit tests. Storage is faked here; real-store assertions live in integration tests."""

import asyncio
import importlib
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from filelock import FileLock, Timeout

from cognee_community_connector_read_ai.auth import ISSUER, OAuthIdentity
from cognee_community_connector_read_ai.journal import atomic_write, read_state
from cognee_community_connector_read_ai.models import (
    AccountMismatchError,
    BudgetExceededError,
    ConfigurationError,
    IncompleteSyncError,
    Selection,
    Snapshot,
    SyncLimits,
    UnsupportedBackendError,
    VerificationError,
)
from cognee_community_connector_read_ai.read_ai import SourceConfig

sync_module = importlib.import_module("cognee_community_connector_read_ai.sync")


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    identity = OAuthIdentity(ISSUER, "subject")
    row = {"id": "meeting", "title": "Title", "content": "Current", "url": "", "_deleted": False}
    fake = SimpleNamespace(
        snapshot=Snapshot(
            {"meeting": {"availability": {"summary": "available"}}}, {"meeting": row}, [], identity
        ),
        records=[],
        pending=False,
        calls=[],
        failure=None,
        expected_identities=[],
        begin_add=None,
        release_add=None,
        api_error=None,
        backend_error=None,
        recovered_state=None,
    )
    config = SourceConfig(
        "connection", object(), None, Selection(), SyncLimits(), tmp_path / "private"
    )
    source = SimpleNamespace(_read_ai_config=config)
    fake.source = source
    fake.user = SimpleNamespace(id="user-id")
    fake.dataset = SimpleNamespace(id="dataset-id", name="dataset", owner_id=fake.user.id)

    def backends():
        fake.calls.append("check_backends")
        if fake.backend_error:
            raise fake.backend_error

    monkeypatch.setattr(sync_module.compat, "check_backends", backends)
    monkeypatch.setattr(sync_module.compat, "storage_namespace", lambda: str(tmp_path / "storage"))
    fake.path = sync_module._context(source, fake.dataset, fake.user)[3]
    fake.lock_path = sync_module._context(source, fake.dataset, fake.user)[1]

    class FakeAPI:
        def __init__(self, *args, **kwargs):
            self.deadline = time.monotonic() + source._read_ai_config.limits.max_duration_seconds

        def reconcile(self, selection, previous_ids, expected_identity, *, deadline=None):
            fake.calls.append("reconcile")
            fake.expected_identities.append(expected_identity)
            if deadline is not None:
                self.deadline = min(self.deadline, deadline)
            if fake.api_error:
                raise fake.api_error
            if expected_identity is not None and expected_identity != fake.snapshot.identity:
                raise AccountMismatchError("account changed")
            return fake.snapshot

        def check_budget(self):
            if time.monotonic() >= self.deadline:
                raise BudgetExceededError("expired")

        def close(self):
            fake.calls.append("close")

    monkeypatch.setattr(sync_module, "ReadAIClient", FakeAPI)
    monkeypatch.setattr(
        sync_module,
        "_make_source",
        lambda config, table, scope, *, snapshot, rebuild=False: SimpleNamespace(
            snapshot=snapshot, rebuild=rebuild
        ),
    )

    async def authorize(dataset_name, user):
        fake.calls.append("authorize")
        return user or fake.user, fake.dataset

    async def release_handles(dataset):
        fake.calls.append("release_handles")

    async def scoped_data(dataset, table):
        return fake.records

    async def capture(dataset, ids):
        return {"nodes": [], "edges": []}

    def recover(bound, dataset_name):
        fake.calls.append("recover_packages")
        fake.recovered_state = read_state(fake.path)
        if fake.failure == "abort":
            raise IncompleteSyncError("interrupted abort")
        fake.pending = False

    async def add(bound, dataset_name, user, disposition):
        fake.calls.append(("add", disposition, bound.rebuild))
        if fake.begin_add:
            fake.begin_add.set()
            await fake.release_add.wait()
        if fake.failure == "loading":
            raise IncompleteSyncError("interrupted load")
        fake.records = [SimpleNamespace(id="data-" + key) for key in bound.snapshot.rows]

    async def cognify(dataset_id, user):
        fake.calls.append("cognify")
        if fake.failure == "cognify":
            raise IncompleteSyncError("interrupted cognify")

    async def verify(dataset, table, rows, artifacts, **kwargs):
        fake.calls.append("verify")
        if fake.failure == "verify":
            raise VerificationError("cleanup incomplete")
        return fake.records, {"nodes": [], "edges": []}

    monkeypatch.setattr(sync_module.compat, "authorize", authorize)
    monkeypatch.setattr(sync_module.compat, "release_handles", release_handles)
    monkeypatch.setattr(sync_module.compat, "scoped_data", scoped_data)
    monkeypatch.setattr(sync_module.compat, "capture_artifacts", capture)
    monkeypatch.setattr(
        sync_module.compat,
        "pipeline_for",
        lambda *args: SimpleNamespace(has_pending_data=fake.pending),
    )
    monkeypatch.setattr(sync_module.compat, "recover_packages", recover)
    monkeypatch.setattr(sync_module.compat, "add_rows", add)
    monkeypatch.setattr(sync_module.compat, "cognify", cognify)
    monkeypatch.setattr(sync_module.compat, "verify_corpus", verify)
    return fake


async def run(fake):
    return await sync_module.sync_read_ai(fake.source, dataset_name="dataset")


async def test_only_final_verified_generation_advances_success(lifecycle):
    fake = lifecycle
    result = await run(fake)
    successful = read_state(fake.path)["last_success"]
    assert successful["generation"] == result.generation
    assert read_state(fake.path)["attempt"] is None
    assert ("add", "merge", False) in fake.calls
    fake.snapshot.rows["meeting"]["content"] = "Changed"
    fake.failure = "verify"
    with pytest.raises(VerificationError):
        await run(fake)
    state = read_state(fake.path)
    assert state["last_success"] == successful
    assert state["attempt"]["phase"] == "cognified"
    fake.failure = None
    recovered = await run(fake)
    assert recovered.changed == 1
    assert read_state(fake.path)["last_success"]["generation"] == recovered.generation


@pytest.mark.parametrize("failure", ["loading", "cognify", "verify"])
async def test_failed_phases_keep_durable_attempt_without_success(lifecycle, failure):
    fake = lifecycle
    fake.failure = failure
    with pytest.raises(IncompleteSyncError):
        await run(fake)
    failed = read_state(fake.path)
    assert failed["last_success"] is None
    assert failed["attempt"] is not None
    assert (
        failed["attempt"]["phase"]
        == {"loading": "loading", "cognify": "staged", "verify": "cognified"}[failure]
    )
    fake.failure = None
    fake.calls.clear()
    await run(fake)
    assert (
        "add",
        "replace" if failure == "loading" else "merge",
        failure == "loading",
    ) in fake.calls
    assert read_state(fake.path)["attempt"] is None


async def test_pending_packages_mark_rebuild_before_abort_and_abort_failure_is_recoverable(
    lifecycle,
):
    fake = lifecycle
    fake.pending = True
    fake.failure = "abort"
    with pytest.raises(IncompleteSyncError, match="abort"):
        await run(fake)
    assert fake.recovered_state["attempt"]["rebuild_required"] is True
    state = read_state(fake.path)
    assert state["attempt"]["rebuild_required"] is True
    assert state["last_success"] is None
    fake.pending = False  # Simulate abort partially completing before restart.
    fake.failure = "loading"
    with pytest.raises(IncompleteSyncError, match="load"):
        await run(fake)
    assert read_state(fake.path)["attempt"]["rebuild_required"] is True
    fake.failure = None
    fake.calls.clear()
    await run(fake)
    assert ("add", "replace", True) in fake.calls
    assert read_state(fake.path)["attempt"] is None


async def test_incomplete_upstream_does_not_touch_existing_checkpoint_or_storage(lifecycle):
    fake = lifecycle
    await run(fake)
    before = fake.path.read_bytes()
    fake.calls.clear()
    fake.api_error = IncompleteSyncError("page invalid")
    with pytest.raises(IncompleteSyncError):
        await run(fake)
    assert fake.path.read_bytes() == before
    assert not any(isinstance(call, tuple) and call[0] == "add" for call in fake.calls)
    assert "verify" not in fake.calls


async def test_open_ended_selection_is_stable_and_changed_configuration_fails_before_network(
    lifecycle,
):
    fake = lifecycle
    await run(fake)
    assert read_state(fake.path)["configuration"]["selection"]["end_time_ms"] is None
    assert (await run(fake)).unchanged == 1
    fake.source._read_ai_config = replace(
        fake.source._read_ai_config,
        selection=Selection(include_metrics=False),
    )
    fake.calls.clear()
    with pytest.raises(ConfigurationError, match="selection changed"):
        await run(fake)
    assert "reconcile" not in fake.calls


async def test_account_replacement_fails_before_replay_and_preserves_journal(lifecycle):
    fake = lifecycle
    await run(fake)
    before = fake.path.read_bytes()
    fake.snapshot.identity = OAuthIdentity(ISSUER, "replacement-account")
    fake.calls.clear()
    with pytest.raises(AccountMismatchError):
        await run(fake)
    assert fake.path.read_bytes() == before
    assert fake.expected_identities[-1] == OAuthIdentity(ISSUER, "subject")
    assert not any(isinstance(call, tuple) and call[0] == "add" for call in fake.calls)
    assert "cognify" not in fake.calls


async def test_explicit_id_selection_survives_json_journal_round_trip(lifecycle):
    fake = lifecycle
    fake.source._read_ai_config = replace(
        fake.source._read_ai_config, selection=Selection(meeting_ids=("meeting",))
    )
    await run(fake)
    assert (await run(fake)).unchanged == 1


async def test_implicit_and_explicit_same_cognee_user_share_one_binding(lifecycle):
    fake = lifecycle
    initial = await run(fake)
    explicit = await sync_module.sync_read_ai(fake.source, dataset_name="dataset", user=fake.user)
    assert explicit.unchanged == 1
    assert initial.dataset_id == explicit.dataset_id
    assert len(list(fake.source._read_ai_config.state_dir.glob("connections/*.json"))) == 1
    assert read_state(fake.path)["last_success"]["generation"] == explicit.generation


async def test_state_directory_switch_cannot_reset_an_existing_connection(lifecycle):
    fake = lifecycle
    await run(fake)
    before = fake.path.read_bytes()
    fake.source._read_ai_config = replace(
        fake.source._read_ai_config, state_dir=fake.source._read_ai_config.state_dir / "moved"
    )
    fake.calls.clear()
    with pytest.raises(ConfigurationError, match="bound journal directory"):
        await run(fake)
    assert fake.path.read_bytes() == before
    assert "reconcile" not in fake.calls
    assert not any(isinstance(call, tuple) and call[0] == "add" for call in fake.calls)


async def test_unfinished_retirement_cannot_be_undone_by_regular_sync(lifecycle):
    fake = lifecycle
    await run(fake)
    state = read_state(fake.path)
    state["attempt"] = {"phase": "retiring", "rebuild_required": True}
    atomic_write(fake.path, state)
    fake.calls.clear()
    with pytest.raises(ConfigurationError, match="retirement is unfinished"):
        await run(fake)
    assert "reconcile" not in fake.calls
    assert read_state(fake.path)["attempt"]["phase"] == "retiring"


async def test_unsupported_backend_rejected_before_network_or_journal(lifecycle):
    fake = lifecycle
    fake.backend_error = UnsupportedBackendError("unsupported backend")
    with pytest.raises(UnsupportedBackendError):
        await run(fake)
    assert fake.calls == ["check_backends"]
    assert not fake.path.exists()


async def test_recovery_reconciles_visibility_before_replaying_failed_load(lifecycle):
    fake = lifecycle
    fake.failure = "loading"
    with pytest.raises(IncompleteSyncError):
        await run(fake)
    assert read_state(fake.path)["attempt"]["meetings"]
    fake.snapshot = Snapshot({}, {}, [], fake.snapshot.identity)
    fake.failure = None
    result = await run(fake)
    assert result.data_ids == ()
    assert fake.records == []
    assert read_state(fake.path)["last_success"]["meetings"] == {}


async def test_permission_loss_before_recovery_preserves_failed_attempt(lifecycle):
    fake = lifecycle
    fake.failure = "loading"
    with pytest.raises(IncompleteSyncError):
        await run(fake)
    before = fake.path.read_bytes()
    fake.failure = None
    fake.api_error = IncompleteSyncError("visibility cannot be established")
    fake.calls.clear()
    with pytest.raises(IncompleteSyncError):
        await run(fake)
    assert fake.path.read_bytes() == before
    assert not any(isinstance(call, tuple) and call[0] == "add" for call in fake.calls)


def assert_process_lock_held(path):
    lock = FileLock(str(path))
    with pytest.raises(Timeout):
        lock.acquire(blocking=False)


async def test_external_cancellation_waits_for_active_work_before_releasing_lock(lifecycle):
    fake = lifecycle
    fake.begin_add = asyncio.Event()
    fake.release_add = asyncio.Event()
    task = asyncio.create_task(run(fake))
    await asyncio.wait_for(fake.begin_add.wait(), timeout=5)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()
    assert_process_lock_held(fake.lock_path)
    fake.release_add.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert read_state(fake.path)["last_success"] is None
    with FileLock(str(fake.lock_path), timeout=0):
        pass


async def test_deadline_waits_for_active_work_before_releasing_lock(lifecycle):
    fake = lifecycle
    fake.source._read_ai_config = replace(
        fake.source._read_ai_config, limits=SyncLimits(max_duration_seconds=0.2)
    )
    fake.begin_add = asyncio.Event()
    fake.release_add = asyncio.Event()
    task = asyncio.create_task(run(fake))
    await asyncio.wait_for(fake.begin_add.wait(), timeout=5)
    await asyncio.sleep(0.25)
    assert not task.done()
    assert_process_lock_held(fake.lock_path)
    fake.release_add.set()
    with pytest.raises(BudgetExceededError):
        await task
    assert read_state(fake.path)["last_success"] is None
    with FileLock(str(fake.lock_path), timeout=0):
        pass


async def test_retirement_rejects_dataset_rebinding(lifecycle):
    fake = lifecycle
    await run(fake)
    state = read_state(fake.path)
    state["dataset_id"] = "another-dataset"
    atomic_write(fake.path, state)
    with pytest.raises(ConfigurationError, match="different dataset"):
        await sync_module.retire_read_ai(fake.source, dataset_name="dataset")
