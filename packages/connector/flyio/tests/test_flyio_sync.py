"""Tests for Fly.io incremental sync, fingerprinting, and deletion semantics."""

from cognee_community_connector_flyio.flyio import _iter_rows
from fake_flyio import FakeFlyio


def test_incremental_sync_skips_unchanged():
    fake = FakeFlyio()
    fake.add_app("user-service")
    fake.add_machine("user-service", "m_user", "mach-user", region="fra")

    state = {}
    stats1 = {}
    rows1 = list(_iter_rows(fake, state, stats1, include=("apps", "machines")))
    assert len(rows1) == 2
    assert stats1["scanned"] == 2
    assert stats1["skipped"] == 0

    # Second run without changes -> all skipped
    stats2 = {}
    rows2 = list(_iter_rows(fake, state, stats2, include=("apps", "machines")))
    assert len(rows2) == 0
    assert stats2["scanned"] == 2
    assert stats2["skipped"] == 2


def test_deletion_emits_tombstone():
    fake = FakeFlyio()
    fake.add_app("user-service")
    fake.add_machine("user-service", "m_user", "mach-user", region="fra")

    state = {}
    stats1 = {}
    list(_iter_rows(fake, state, stats1, include=("apps", "machines")))

    # Destroy machine
    fake.machines["user-service"] = []

    stats2 = {}
    rows2 = list(_iter_rows(fake, state, stats2, include=("apps", "machines")))
    assert len(rows2) == 1
    assert rows2[0]["id"] == "machine:user-service:m_user"
    assert rows2[0]["_deleted"] is True
    assert stats2["deleted"] == 1


def test_request_budget_cutoff():
    fake = FakeFlyio()
    fake.add_app("user-service")
    fake.add_machine("user-service", "m_user", "mach-user", region="fra")

    state = {}
    stats = {}
    # With max_requests=1, it fetches apps and cuts short before machines
    rows = list(_iter_rows(fake, state, stats, include=("apps", "machines"), max_requests=1))
    assert len(rows) == 1
    assert rows[0]["id"] == "app:user-service"
    assert stats.get("failed_budget") == 1
