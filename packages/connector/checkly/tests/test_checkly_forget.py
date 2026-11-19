"""Tests for forget-on-delete behavior in the Checkly connector."""

from fake_checkly import FakeCheckly

from cognee_community_connector_checkly.checkly import (
    ChecklyAPIError,
    _iter_rows,
)


def _sync(fake: FakeCheckly, state: dict, **kwargs) -> tuple[list[dict], dict]:
    stats: dict[str, int] = {}
    rows = list(_iter_rows(fake, "https://app.checklyhq.com", state, stats, **kwargs))
    return rows, stats


def test_deleted_check_emits_tombstone():
    fake = FakeCheckly()
    fake.add_check("c1", "Check 1")
    fake.add_check("c2", "Check 2")
    state = {}

    rows1, _ = _sync(fake, state)
    assert len(rows1) == 2

    # Delete check 1 in Checkly
    del fake.checks["c1"]

    rows2, stats2 = _sync(fake, state)
    tombstones = [r for r in rows2 if r.get("_deleted")]
    assert len(tombstones) == 1
    assert tombstones[0] == {"id": "check:c1", "_deleted": True}
    assert stats2["deleted"] == 1
    assert "check:c1" not in state["fingerprints"]


def test_deleted_group_and_alert_channel_emit_tombstones():
    fake = FakeCheckly()
    fake.add_check_group("g1", "Group 1")
    fake.add_alert_channel("a1", "Slack Channel")
    state = {}

    _sync(fake, state)
    assert len(state["fingerprints"]) == 2

    del fake.check_groups["g1"]
    del fake.alert_channels["a1"]

    rows2, stats2 = _sync(fake, state)
    tombstone_ids = {r["id"] for r in rows2 if r.get("_deleted")}
    assert tombstone_ids == {"check_group:g1", "alert_channel:a1"}
    assert stats2["deleted"] == 2
    assert "check_group:g1" not in state["fingerprints"]
    assert "alert_channel:a1" not in state["fingerprints"]


def test_deselected_kind_forgets_stored_records():
    fake = FakeCheckly()
    fake.add_check("c1", "Check 1")
    fake.add_alert_channel("a1", "Alert 1")
    state = {}

    _sync(fake, state, include=("checks", "alert_channels"))
    assert len(state["fingerprints"]) == 2

    # Re-run including only checks
    rows2, _ = _sync(fake, state, include=("checks",))
    tombstones = [r for r in rows2 if r.get("_deleted")]
    assert len(tombstones) == 1
    assert tombstones[0]["id"] == "alert_channel:a1"
    assert "alert_channel:a1" not in state["fingerprints"]


def test_tag_removed_from_check_forgets_it_when_filtered():
    fake = FakeCheckly()
    fake.add_check("c1", "Check 1", tags=["prod"])
    state = {}

    _sync(fake, state, check_tags=["prod"])
    assert "check:c1" in state["fingerprints"]

    # Tag removed upstream
    fake.checks["c1"]["tags"] = ["dev"]

    rows2, _ = _sync(fake, state, check_tags=["prod"])
    tombstones = [r for r in rows2 if r.get("_deleted")]
    assert len(tombstones) == 1
    assert tombstones[0]["id"] == "check:c1"
    assert "check:c1" not in state["fingerprints"]


def test_transient_error_during_deletion_check_preserves_record():
    fake = FakeCheckly()
    fake.add_check("c1", "Check 1")
    state = {}

    _sync(fake, state)
    del fake.checks["c1"]

    fake.fail_paths["/checks/c1"] = ChecklyAPIError("Checkly server error")

    rows2, stats2 = _sync(fake, state)
    assert len(rows2) == 0
    assert stats2["failed"] == 1
    assert "check:c1" in state["fingerprints"]
