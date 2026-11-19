"""The required foreground sync boundary, with explicit durable acknowledgement."""

import asyncio
import json
import time
import traceback
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from filelock import FileLock, Timeout

from . import compat
from .auth import OAuthIdentity
from .client import ReadAIClient
from .journal import atomic_write, digest, read_state
from .models import (
    AccountMismatchError,
    BudgetExceededError,
    ConfigurationError,
    Snapshot,
    SyncResult,
)
from .read_ai import _make_source, rate_directory


@asynccontextmanager
async def _lock(path, deadline):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = FileLock(str(path), thread_local=False)
    while True:
        if time.monotonic() >= deadline:
            raise BudgetExceededError("Waiting for the dataset synchronization lock timed out")
        try:
            lock.acquire(blocking=False)
            break
        except Timeout:
            await asyncio.sleep(min(0.05, max(0, deadline - time.monotonic())))
    try:
        yield
    finally:
        lock.release()


async def _settled(awaitable, deadline):
    """Never release a process lock while a core to_thread operation still runs."""
    task = asyncio.ensure_future(awaitable)
    try:
        remaining = max(0, deadline - time.monotonic())
        return await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
    except (asyncio.CancelledError, TimeoutError) as error:
        # Cognee runs dlt on a worker thread. Cancellation of its asyncio
        # wrapper would not stop that thread; finish this phase before unlock.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if task.done() and not task.cancelled():
            task.exception()  # consume a settled failure without hiding cancellation
        if isinstance(error, asyncio.CancelledError):
            raise
        raise BudgetExceededError(
            "Sync deadline expired; active storage work has settled"
        ) from error


def _config(source, dataset_name):
    config = getattr(source, "_read_ai_config", None)
    if config is None or not isinstance(dataset_name, str) or not dataset_name.strip():
        raise ConfigurationError("Pass read_ai_source(...) and a nonempty dataset_name")
    return config


def _context(source, dataset, user):
    config = source._read_ai_config
    namespace = [compat.storage_namespace(), str(dataset.id)]
    dataset_key = digest(namespace)
    connection_key = digest([*namespace, config.connection_id])
    base = config.state_dir
    canonical = Path(compat.storage_namespace()) / "read_ai_sync" / "locks"
    return (
        config,
        canonical / f"dataset-{dataset_key}.lock",
        canonical / f"connection-{connection_key}.lock",
        base / "connections" / f"{connection_key}.json",
    )


@asynccontextmanager
async def _session(source, dataset_name, user):
    config = _config(source, dataset_name)
    compat.check_backends()
    deadline = time.monotonic() + config.limits.max_duration_seconds
    canonical = Path(compat.storage_namespace()) / "read_ai_sync" / "locks"
    # SQLite startup and a shared native Ladybug store also need coordination
    # before dataset resolution. This small local v1 serializes Read.ai runs
    # against one physical Cognee installation, including different datasets.
    async with _lock(canonical / "storage.lock", deadline):
        resolved_user, dataset = await _settled(compat.authorize(dataset_name, user), deadline)
        _, dataset_lock, connection_lock, path = _context(source, dataset, resolved_user)
        async with _lock(dataset_lock, deadline), _lock(connection_lock, deadline):
            binding_path = connection_lock.with_suffix(".binding.json")
            if binding_path.exists():
                try:
                    binding = json.loads(binding_path.read_text())
                except (OSError, ValueError) as error:
                    raise ConfigurationError(
                        "Cannot read the canonical connection binding"
                    ) from error
                if binding.get("state_path") != str(path) or binding.get("user_id") != str(
                    resolved_user.id
                ):
                    raise ConfigurationError(
                        "Keep the bound journal directory and Cognee user fixed"
                    )
                if not path.exists():
                    raise ConfigurationError(
                        "The bound journal is missing; restore it before syncing"
                    )
            try:
                yield config, path, binding_path, resolved_user, dataset, deadline
            except BaseException as error:
                # Failed core frames can retain leased native handles through
                # their traceback. Drop only frame locals, preserving the error
                # and stack locations, before closing caches and unlocking.
                traceback.clear_frames(error.__traceback__)
                raise
            finally:
                await _settled(compat.release_handles(dataset), float("inf"))


def _check_state(state, config, *, retiring=False):
    if state.get("retired"):
        raise ConfigurationError("This connection is retired; choose a new connection_id")
    if state.get("configuration") not in (None, config.fingerprint()):
        raise ConfigurationError("The selection changed; retire it and use a new connection_id")
    if not retiring and (state.get("attempt") or {}).get("phase") == "retiring":
        raise ConfigurationError("Connection retirement is unfinished; resume retire_read_ai")


def _counts(snapshot, last):
    previous = (last or {}).get("hashes", {})
    hashes = {key: digest(row) for key, row in snapshot.rows.items()}
    return {
        "created": len(set(hashes) - set(previous)),
        "changed": sum(hashes[key] != previous[key] for key in hashes.keys() & previous.keys()),
        "unchanged": sum(hashes[key] == previous[key] for key in hashes.keys() & previous.keys()),
        "forgotten": len(set((last or {}).get("meetings", {})) - set(snapshot.meetings)),
        "withdrawn": len((set(previous) - set(hashes)) & set(snapshot.meetings)),
        "pending": len(snapshot.pending),
        "unavailable_sections": sum(
            status == "unavailable"
            for meeting in snapshot.meetings.values()
            for status in meeting.get("availability", {}).values()
        ),
    }


async def sync_read_ai(source, *, dataset_name, user=None):
    async with _session(source, dataset_name, user) as context:
        config, path, binding_path, resolved_user, dataset, deadline = context
        state = read_state(path)
        _check_state(state, config)
        last = state.get("last_success")
        unfinished = state.get("attempt") or {}
        expected_identity = OAuthIdentity(**state["identity"]) if state.get("identity") else None
        previous_ids = set((last or {}).get("meetings", {})) | set(unfinished.get("meetings", {}))
        api = ReadAIClient(
            config.token_provider,
            client=config.client,
            limits=config.limits,
            rate_dir=rate_directory(),
        )
        try:
            # Reconciliation resets its own budget, so keep the outer deadline,
            # including lock wait, as the authority for all phases.
            snapshot = await _settled(
                asyncio.to_thread(
                    api.reconcile,
                    config.selection,
                    previous_ids,
                    expected_identity,
                    deadline=deadline,
                ),
                deadline,
            )
            api.deadline = min(api.deadline, deadline)
            api.check_budget()
            if state.get("dataset_id") not in (None, str(dataset.id)):
                raise ConfigurationError(
                    "The bound Cognee dataset changed; do not replay its journal"
                )
            if state.get("identity") not in (None, asdict(snapshot.identity)):
                raise AccountMismatchError("The credentials differ from the connection identity")
            scope = digest([asdict(snapshot.identity), config.connection_id, str(dataset.id)])
            table = "read_ai_meetings_" + scope[:24]
            bound = _make_source(config, table, scope, snapshot=snapshot)
            records = await _settled(compat.scoped_data(dataset, table), deadline)
            old_ids = {str(record.id) for record in records}
            old_ids.update((last or {}).get("data_ids", []))
            old_ids.update(unfinished.get("data_ids", []))
            current_artifacts = await _settled(compat.capture_artifacts(dataset, old_ids), deadline)
            artifacts = compat.merge_artifacts(
                (last or {}).get("artifacts"), unfinished.get("artifacts"), current_artifacts
            )
            rebuild = bool(
                unfinished.get("rebuild_required") or unfinished.get("phase") == "loading"
            )
            attempt = {
                "generation": str(uuid4()),
                "phase": "prepared",
                "meetings": snapshot.meetings,
                "hashes": {key: digest(row) for key, row in snapshot.rows.items()},
                "data_ids": sorted(old_ids),
                "artifacts": artifacts,
                "rebuild_required": rebuild,
            }
            state.update(
                configuration=config.fingerprint(),
                identity=asdict(snapshot.identity),
                dataset_id=str(dataset.id),
                table_name=table,
                pipeline_scope=scope,
                attempt=attempt,
            )
            atomic_write(path, state)
            atomic_write(binding_path, {"state_path": str(path), "user_id": str(resolved_user.id)})
            # Before aborting anything, durably record that a partially modified
            # destination MUST be replaced even if this process dies mid-abort.
            pending = await _settled(
                asyncio.to_thread(
                    lambda: compat.pipeline_for(bound, dataset.name).has_pending_data
                ),
                deadline,
            )
            if pending:
                rebuild = attempt["rebuild_required"] = True
                atomic_write(path, state)
                await _settled(
                    asyncio.to_thread(compat.recover_packages, bound, dataset.name), deadline
                )
            if rebuild:
                bound = _make_source(config, table, scope, snapshot=snapshot, rebuild=True)
            attempt["phase"] = "loading"
            atomic_write(path, state)
            api.check_budget()
            await _settled(
                compat.add_rows(
                    bound, dataset.name, resolved_user, "replace" if rebuild else "merge"
                ),
                deadline,
            )
            # Retained-table readback in core recovers unchanged data without a
            # parallel re-ingestion system. Rebuild remains marked until ack.
            attempt["phase"] = "staged"
            records = await _settled(compat.scoped_data(dataset, table), deadline)
            attempt["data_ids"] = sorted(old_ids | {str(record.id) for record in records})
            atomic_write(path, state)
            api.check_budget()
            if records:
                await _settled(compat.cognify(dataset.id, resolved_user), deadline)
            attempt["phase"] = "cognified"
            atomic_write(path, state)
            api.check_budget()
            records, new_artifacts = await _settled(
                compat.verify_corpus(dataset, table, snapshot.rows, artifacts), deadline
            )
            api.check_budget()
            generation = attempt["generation"]
            result = SyncResult(
                **_counts(snapshot, last),
                generation=generation,
                dataset_id=str(dataset.id),
                data_ids=tuple(str(record.id) for record in records),
            )
            state["last_success"] = {
                "generation": generation,
                "meetings": snapshot.meetings,
                "hashes": attempt["hashes"],
                "data_ids": list(result.data_ids),
                "artifacts": new_artifacts,
                "completed_at": time.time(),
            }
            state["attempt"] = None
            atomic_write(path, state)
            return result
        finally:
            api.close()


async def retire_read_ai(source, *, dataset_name, user=None):
    """Explicit local retirement, even when upstream authorization is revoked."""
    async with _session(source, dataset_name, user) as context:
        config, path, _binding_path, resolved_user, dataset, deadline = context
        state = read_state(path)
        if state.get("retired"):
            return
        if not state.get("identity") or not state.get("dataset_id"):
            raise ConfigurationError("There is no bound connection to retire")
        _check_state(state, config, retiring=True)
        if str(dataset.id) != state["dataset_id"]:
            raise ConfigurationError("Retirement cannot target a different dataset")
        table = state["table_name"]
        records = await _settled(compat.scoped_data(dataset, table), deadline)
        data_ids = {str(record.id) for record in records}
        last, old_attempt = state.get("last_success") or {}, state.get("attempt") or {}
        data_ids.update(last.get("data_ids", []))
        data_ids.update(old_attempt.get("data_ids", []))
        artifacts = compat.merge_artifacts(
            last.get("artifacts"),
            old_attempt.get("artifacts"),
            await _settled(compat.capture_artifacts(dataset, data_ids), deadline),
        )
        state["attempt"] = {
            "generation": str(uuid4()),
            "phase": "retiring",
            "rebuild_required": True,
            "data_ids": sorted(data_ids),
            "artifacts": artifacts,
            "meetings": {},
        }
        atomic_write(path, state)
        snapshot = Snapshot({}, {}, [], OAuthIdentity(**state["identity"]))
        bound = _make_source(
            config, table, state["pipeline_scope"], snapshot=snapshot, rebuild=True
        )
        await _settled(asyncio.to_thread(compat.recover_packages, bound, dataset.name), deadline)
        await _settled(compat.repair_record_cleanup(dataset, data_ids, [], resolved_user), deadline)
        await _settled(compat.add_rows(bound, dataset.name, resolved_user, "replace"), deadline)
        await _settled(compat.verify_corpus(dataset, table, {}, artifacts), deadline)
        if time.monotonic() >= deadline:
            raise BudgetExceededError("Retirement exceeded its deadline")
        state.update(retired=True, attempt=None, last_success=None)
        atomic_write(path, state)
