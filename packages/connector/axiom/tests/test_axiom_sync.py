"""Tests for Axiom incremental sync, fingerprinting, and deletion semantics."""

from cognee_community_connector_axiom.axiom import _iter_rows
from fake_axiom import FakeAxiom


def test_incremental_sync_skips_unchanged():
    fake = FakeAxiom()
    fake.add_dataset("app-logs")
    fake.add_monitor("mon_latency", "High Latency", "['app-logs'] | where duration > 1000")

    state = {}
    stats1 = {}
    rows1 = list(_iter_rows(fake, state, stats1, include=("datasets", "monitors")))
    assert len(rows1) == 2
    assert stats1["scanned"] == 2
    assert stats1["skipped"] == 0

    # Second run without changes -> all skipped
    stats2 = {}
    rows2 = list(_iter_rows(fake, state, stats2, include=("datasets", "monitors")))
    assert len(rows2) == 0
    assert stats2["scanned"] == 2
    assert stats2["skipped"] == 2


def test_deletion_emits_tombstone():
    fake = FakeAxiom()
    fake.add_dataset("app-logs")
    fake.add_monitor("mon_latency", "High Latency", "['app-logs'] | where duration > 1000")

    state = {}
    stats1 = {}
    list(_iter_rows(fake, state, stats1, include=("datasets", "monitors")))

    # Delete monitor
    fake.monitors = []

    stats2 = {}
    rows2 = list(_iter_rows(fake, state, stats2, include=("datasets", "monitors")))
    assert len(rows2) == 1
    assert rows2[0]["id"] == "monitor:mon_latency"
    assert rows2[0]["_deleted"] is True
    assert stats2["deleted"] == 1


def test_request_budget_cutoff():
    fake = FakeAxiom()
    fake.add_dataset("app-logs")
    fake.add_monitor("mon_latency", "High Latency", "['app-logs'] | where duration > 1000")

    state = {}
    stats = {}
    # With max_requests=1, it fetches datasets and cuts short before monitors
    rows = list(_iter_rows(fake, state, stats, include=("datasets", "monitors"), max_requests=1))
    assert len(rows) == 1
    assert rows[0]["id"] == "dataset:app-logs"
    assert stats.get("failed_budget") == 1
