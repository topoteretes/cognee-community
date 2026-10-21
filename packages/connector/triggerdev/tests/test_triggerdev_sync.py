"""Tests for Trigger.dev incremental sync, pagination, and deletion semantics."""

from cognee_community_connector_triggerdev.triggerdev import _iter_rows
from fake_triggerdev import FakeTriggerDev


def test_incremental_sync_skips_unchanged():
    fake = FakeTriggerDev()
    fake.add_task("task-1")
    fake.add_schedule("sched-1", "task-1")

    state = {}
    stats1 = {}
    rows1 = list(_iter_rows(fake, state, stats1, include=("tasks", "schedules")))
    assert len(rows1) == 2
    assert stats1["scanned"] == 2
    assert stats1["skipped"] == 0

    # Second run without changes -> all skipped, 0 emitted
    stats2 = {}
    rows2 = list(_iter_rows(fake, state, stats2, include=("tasks", "schedules")))
    assert len(rows2) == 0
    assert stats2["scanned"] == 2
    assert stats2["skipped"] == 2


def test_deletion_emits_tombstone():
    fake = FakeTriggerDev()
    fake.add_task("task-1")
    fake.add_schedule("sched-1", "task-1")

    state = {}
    stats1 = {}
    list(_iter_rows(fake, state, stats1, include=("tasks", "schedules")))

    # Delete sched-1 from Trigger.dev
    del fake.schedules["sched-1"]

    stats2 = {}
    rows2 = list(_iter_rows(fake, state, stats2, include=("tasks", "schedules")))
    # Sched-1 is gone, so a tombstone is emitted
    assert len(rows2) == 1
    assert rows2[0]["id"] == "schedule:sched-1"
    assert rows2[0]["_deleted"] is True
    assert stats2.get("deleted") == 1


def test_runs_pagination():
    fake = FakeTriggerDev()
    for i in range(150):
        fake.add_run(f"run-{i}", "worker-task")

    state = {}
    stats = {}
    rows = list(_iter_rows(fake, state, stats, include=("runs",)))
    assert len(rows) == 150
    assert stats["scanned"] == 150
