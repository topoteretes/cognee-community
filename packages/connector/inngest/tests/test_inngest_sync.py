"""Tests for Inngest incremental sync, fingerprinting, and deletion semantics."""

from cognee_community_connector_inngest.inngest import _iter_rows
from fake_inngest import FakeInngest


def test_incremental_sync_skips_unchanged():
    fake = FakeInngest()
    fake.add_function("order-processor", "Order Processor")
    fake.add_event("shop/order.placed")

    state = {}
    stats1 = {}
    rows1 = list(_iter_rows(fake, state, stats1, include=("functions", "events")))
    assert len(rows1) == 2
    assert stats1["scanned"] == 2
    assert stats1["skipped"] == 0

    # Second run without changes -> all skipped
    stats2 = {}
    rows2 = list(_iter_rows(fake, state, stats2, include=("functions", "events")))
    assert len(rows2) == 0
    assert stats2["scanned"] == 2
    assert stats2["skipped"] == 2


def test_deletion_emits_tombstone():
    fake = FakeInngest()
    fake.add_function("order-processor", "Order Processor")
    fake.add_event("shop/order.placed")

    state = {}
    stats1 = {}
    list(_iter_rows(fake, state, stats1, include=("functions", "events")))

    # Delete function
    fake.functions = []

    stats2 = {}
    rows2 = list(_iter_rows(fake, state, stats2, include=("functions", "events")))
    assert len(rows2) == 1
    assert rows2[0]["id"] == "function:order-processor"
    assert rows2[0]["_deleted"] is True
    assert stats2["deleted"] == 1


def test_request_budget_cutoff():
    fake = FakeInngest()
    fake.add_function("order-processor", "Order Processor")
    fake.add_event("shop/order.placed")

    state = {}
    stats = {}
    # With max_requests=1, it fetches functions and cuts short before events
    rows = list(_iter_rows(fake, state, stats, include=("functions", "events"), max_requests=1))
    assert len(rows) == 1
    assert rows[0]["id"] == "function:order-processor"
    assert stats.get("failed_budget") == 1
