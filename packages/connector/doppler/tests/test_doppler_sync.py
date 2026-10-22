"""Tests for Doppler incremental sync, fingerprinting, and deletion semantics."""

from cognee_community_connector_doppler.doppler import _iter_rows
from fake_doppler import FakeDoppler


def test_incremental_sync_skips_unchanged():
    fake = FakeDoppler()
    fake.add_project("auth-service")
    fake.add_environment("auth-service", "dev", "Development")

    state = {}
    stats1 = {}
    rows1 = list(_iter_rows(fake, state, stats1, include=("projects", "environments")))
    assert len(rows1) == 2
    assert stats1["scanned"] == 2
    assert stats1["skipped"] == 0

    # Second run without changes -> all skipped
    stats2 = {}
    rows2 = list(_iter_rows(fake, state, stats2, include=("projects", "environments")))
    assert len(rows2) == 0
    assert stats2["scanned"] == 2
    assert stats2["skipped"] == 2


def test_deletion_emits_tombstone():
    fake = FakeDoppler()
    fake.add_project("auth-service")
    fake.add_environment("auth-service", "dev", "Development")

    state = {}
    stats1 = {}
    list(_iter_rows(fake, state, stats1, include=("projects", "environments")))

    # Delete environment dev
    fake.environments["auth-service"] = []

    stats2 = {}
    rows2 = list(_iter_rows(fake, state, stats2, include=("projects", "environments")))
    assert len(rows2) == 1
    assert rows2[0]["id"] == "environment:auth-service:dev"
    assert rows2[0]["_deleted"] is True
    assert stats2["deleted"] == 1
