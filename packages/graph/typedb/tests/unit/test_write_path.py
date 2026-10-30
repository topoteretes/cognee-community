"""Offline tests of the batch write/read paths (no server: the executor is faked)."""

import asyncio

import pytest

from cognee_community_graph_adapter_typedb.provenance import _ProvenanceAttach
from cognee_community_graph_adapter_typedb.typedb_adapter import (
    WRITE_CHUNK_ROWS,
    WRITE_CONCURRENCY,
    TypeDBAdapter,
)

STC2 = "\n[STC2] Commit in database 'x' failed with isolation conflict: ..."


def _offline_adapter() -> TypeDBAdapter:
    adapter = TypeDBAdapter()
    adapter._schema_initialized = True  # skip the server round trip
    # The tests size their batches from the module defaults.
    adapter._chunk_rows, adapter._write_concurrency = WRITE_CHUNK_ROWS, WRITE_CONCURRENCY
    return adapter


async def test_write_batch_retries_commit_conflicts_then_succeeds():
    adapter = _offline_adapter()
    calls = []

    async def fake_run_sync(fn, *args):
        calls.append(args)
        if len(calls) < 3:
            raise RuntimeError(STC2)

    adapter._run_sync = fake_run_sync
    await adapter._write_batch(["match $n isa node; delete $n;"])
    assert len(calls) == 3


async def test_write_batch_does_not_retry_other_errors():
    adapter = _offline_adapter()
    calls = []

    async def fake_run_sync(fn, *args):
        calls.append(1)
        raise RuntimeError("[CNT5] Constraint violated")

    adapter._run_sync = fake_run_sync
    with pytest.raises(RuntimeError, match="CNT5"):
        await adapter._write_batch(["match $n isa node; delete $n;"])
    assert len(calls) == 1


async def test_write_batch_gives_up_when_the_conflict_budget_ends():
    adapter = _offline_adapter()
    adapter._commit_retry_seconds = 0.15
    calls = []

    async def fake_run_sync(fn, *args):
        calls.append(1)
        raise RuntimeError(STC2)

    adapter._run_sync = fake_run_sync
    with pytest.raises(RuntimeError, match="STC2"):
        await adapter._write_batch(["match $n isa node; delete $n;"])
    # Time-budgeted, not count-budgeted: several rounds within 150 ms.
    assert len(calls) >= 3


async def test_write_rows_cancels_pending_chunks_on_first_failure():
    adapter = _offline_adapter()
    started = []

    async def fake_write_batch(specs, retry=True):
        first_id = specs[0][1][0]["id"]
        started.append(first_id)
        if first_id == "0":
            raise RuntimeError("chunk 0 failed")
        await asyncio.sleep(0.2)  # in flight while chunk 0 fails

    adapter._write_batch = fake_write_batch
    rows = [{"id": str(i)} for i in range(WRITE_CHUNK_ROWS * (WRITE_CONCURRENCY + 2))]

    with pytest.raises(RuntimeError, match="chunk 0 failed"):
        await adapter._write_rows("template", rows)

    # The slot freed by the failing chunk can be grabbed by the next waiter in
    # the same loop tick before gather() cancels, so at most one extra chunk
    # starts; the remaining pending chunk must never run.
    assert len(started) <= WRITE_CONCURRENCY + 1
    assert str(WRITE_CHUNK_ROWS * (WRITE_CONCURRENCY + 1)) not in started


async def test_write_rows_retries_a_conflicting_chunk_without_holding_a_slot():
    adapter = _offline_adapter()
    attempts = {}

    async def fake_write_batch(specs, retry=True):
        first_id = specs[0][1][0]["id"]
        attempts[first_id] = attempts.get(first_id, 0) + 1
        if first_id == "0" and attempts[first_id] < 3:
            raise RuntimeError(STC2)

    adapter._write_batch = fake_write_batch
    rows = [{"id": str(i)} for i in range(WRITE_CHUNK_ROWS * 2)]
    await adapter._write_rows("template", rows)
    assert attempts["0"] == 3
    assert attempts[str(WRITE_CHUNK_ROWS)] == 1


async def test_reads_short_circuit_when_the_database_is_missing():
    """Reads must not provision: a missing database yields empty results."""
    adapter = TypeDBAdapter()
    calls = []

    async def fake_run_sync(fn, *args):
        calls.append(getattr(fn, "__name__", repr(fn)))
        if fn.__name__ == "_database_exists_sync":
            return False
        raise AssertionError("no transaction should run against a missing database")

    adapter._run_sync = fake_run_sync
    assert await adapter.is_empty() is True
    assert await adapter.has_node("x") is False
    assert await adapter.get_graph_data() == ([], [])
    assert await adapter.query("match $n isa node; reduce $c = count;") == []
    assert set(calls) == {"_database_exists_sync"}


def _attach(with_refs: bool = False):
    """A provenance change with a real key and run ref (the ref put derives
    dataset and run ids from them) and a no-op transition."""
    from uuid import uuid4

    from cognee.infrastructure.databases.provenance import (
        make_source_ref_key,
        make_source_run_ref,
    )

    if not with_refs:
        return _ProvenanceAttach(lambda current, refs: None)
    key, run = make_source_ref_key(uuid4(), uuid4()), uuid4()
    return _ProvenanceAttach(lambda current, refs: None, [key], [make_source_run_ref(run, key)])


async def test_provenance_batches_put_refs_first_then_fold_each_chunk():
    """With provenance, the ref entities are put in their own transaction
    before any chunk, then every chunk is one transaction (upsert + attach)
    and chunks run concurrently like any other write."""
    adapter = _offline_adapter()
    in_flight, max_in_flight, folded, batches = 0, 0, [], []

    async def fake_run_sync(fn, *args):
        nonlocal in_flight, max_in_flight
        if fn.__name__ != "_provenance_change_sync":
            if fn.__name__ == "_run_batch_sync":
                batches.append([query for query, _rows in args[0]])
                assert not folded  # refs are put before the first chunk
            return []
        kind, identities, _transition, specs = args
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        folded.append((kind, identities, [row["id"] for row in specs[0][1]]))

    adapter._run_sync = fake_run_sync
    rows = [{"id": str(i)} for i in range(WRITE_CHUNK_ROWS * 3)]
    await adapter._write_rows("template", rows, provenance=("node", "id", _attach(True)))

    assert len(batches) == 1 and len(batches[0]) == 2  # one put for refs, one for run refs
    assert max_in_flight > 1
    assert sorted(len(ids) for _, ids, _ in folded) == [WRITE_CHUNK_ROWS] * 3
    assert all(ids == upserted for _, ids, upserted in folded)  # attach covers the chunk's rows


async def test_provenance_chunk_failure_stops_the_batch():
    """A failing provenance chunk propagates; later chunks never start (so a
    partial batch is a prefix of committed, fully-stamped chunks)."""
    adapter = _offline_adapter()
    seen = []

    async def fake_run_sync(fn, *args):
        if fn.__name__ != "_provenance_change_sync":
            return []
        seen.append(args[1][0])
        if len(seen) == 2:
            raise RuntimeError("chunk 2 failed")
        await asyncio.sleep(0.05)  # still in flight when the failure is observed

    adapter._run_sync = fake_run_sync
    rows = [{"id": str(i)} for i in range(WRITE_CHUNK_ROWS * (WRITE_CONCURRENCY + 2))]
    with pytest.raises(RuntimeError, match="chunk 2 failed"):
        await adapter._write_rows("template", rows, provenance=("node", "id", _attach()))
    # Chunks already in flight finish. The slot freed by the failing chunk can
    # be grabbed by the next waiter in the same loop tick before gather()
    # cancels, so at most one extra chunk starts; the last one never runs.
    assert len(seen) <= WRITE_CONCURRENCY + 1
    assert str(WRITE_CHUNK_ROWS * (WRITE_CONCURRENCY + 1)) not in seen


class _RecordingTransaction:
    """Stand-in for a driver transaction: records queries and commits."""

    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def query(self, text, given_rows=None):
        self.log.append(("query", text.strip().splitlines()[0], given_rows))
        answer = []

        class _Promise:
            def resolve(self):
                return answer

        return _Promise()

    def commit(self):
        self.log.append(("commit",))


async def test_folded_chunk_is_one_transaction():
    """The upsert and the attach share a transaction: if the attach fails,
    the upsert is never committed."""
    adapter = _offline_adapter()
    log = []

    class _Driver:
        def transaction(self, _database, _type):
            return _RecordingTransaction(log)

    adapter._get_driver = lambda: _Driver()
    adapter._collect_answer = lambda answer: []  # no links yet

    def transition_that_fails(keys, run_refs):
        raise RuntimeError("attach failed")

    with pytest.raises(RuntimeError, match="attach failed"):
        adapter._provenance_change_sync(
            "node", ["n1"], transition_that_fails, pre_specs=[("put $n isa node;", [{"id": "n1"}])]
        )
    assert log[0] == ("query", "put $n isa node;", [{"id": "n1"}])  # upsert was issued ...
    assert ("commit",) not in log  # ... but nothing committed


async def test_cancelled_write_waits_for_the_in_flight_transaction():
    """Cancelling a caller must not let a chunk commit after the caller (and
    cognee's rollback) has moved on: the in-flight transaction finishes first."""
    import time

    adapter = _offline_adapter()
    finished = []

    def slow_transaction():
        time.sleep(0.15)
        finished.append("committed")

    task = asyncio.create_task(adapter._run_sync_shielded(slow_transaction))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished == ["committed"]  # the cancellation waited for it
    await adapter.close()


class _FakeDriver:
    instances: list = []

    def __init__(self):
        self.closed = False
        _FakeDriver.instances.append(self)

    def close(self):
        self.closed = True


def _patch_driver_factory(monkeypatch):
    import typedb.driver

    _FakeDriver.instances.clear()
    monkeypatch.setattr(typedb.driver.TypeDB, "driver", staticmethod(lambda *a, **k: _FakeDriver()))


async def test_close_drains_queued_work_before_closing_the_driver(monkeypatch):
    """A worker queued behind close() must use the driver that is being
    closed, never open a fresh one that would outlive close()."""
    import time

    _patch_driver_factory(monkeypatch)
    adapter = TypeDBAdapter()
    first = adapter._get_driver()
    seen = []

    def queued_work():
        time.sleep(0.1)  # still queued when close() starts draining
        seen.append(adapter._get_driver())

    future = adapter._get_executor().submit(queued_work)
    await adapter.close()
    future.result()

    assert seen == [first]  # the worker used the pre-existing driver
    assert adapter._driver is None
    assert [d.closed for d in _FakeDriver.instances] == [True]  # one driver, closed


async def test_no_driver_can_open_while_closing(monkeypatch):
    """With no driver open, a worker queued behind close() cannot create one
    and is told the adapter is closing; nothing is left open."""
    import time

    _patch_driver_factory(monkeypatch)
    adapter = TypeDBAdapter()
    errors = []

    def queued_work():
        time.sleep(0.1)
        try:
            adapter._get_driver()
        except RuntimeError as error:
            errors.append(str(error))

    future = adapter._get_executor().submit(queued_work)
    await adapter.close()
    future.result()

    assert errors == ["TypeDB adapter is closing"]
    assert _FakeDriver.instances == []
    assert adapter._get_driver() is not None  # reopens lazily afterwards
    adapter._close_sync()
    assert [d.closed for d in _FakeDriver.instances] == [True]


def test_close_sync_has_the_same_ordering(monkeypatch):
    import time

    _patch_driver_factory(monkeypatch)
    adapter = TypeDBAdapter()
    first = adapter._get_driver()
    seen = []
    future = adapter._get_executor().submit(
        lambda: (time.sleep(0.1), seen.append(adapter._get_driver()))
    )
    adapter._close_sync()
    future.result()
    assert seen == [first] and first.closed and adapter._driver is None
    assert len(_FakeDriver.instances) == 1
