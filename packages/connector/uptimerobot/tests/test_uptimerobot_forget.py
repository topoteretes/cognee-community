"""Tests for forget-on-delete behavior in the UptimeRobot connector."""

from fake_uptimerobot import FakeUptimeRobot

from cognee_community_connector_uptimerobot.uptimerobot import (
    UptimeRobotAPIError,
    _iter_rows,
)


def _sync(fake: FakeUptimeRobot, state: dict, **kwargs) -> tuple[list[dict], dict]:
    stats: dict[str, int] = {}
    rows = list(_iter_rows(fake, "https://uptimerobot.com/dashboard", state, stats, **kwargs))
    return rows, stats


def test_deleted_monitor_emits_tombstone():
    fake = FakeUptimeRobot()
    fake.add_monitor("101", "Monitor 1")
    fake.add_monitor("102", "Monitor 2")
    state = {}

    rows1, _ = _sync(fake, state)
    assert len(rows1) == 2

    # Delete monitor 101 in UptimeRobot
    del fake.monitors["101"]

    rows2, stats2 = _sync(fake, state)
    tombstones = [r for r in rows2 if r.get("_deleted")]
    assert len(tombstones) == 1
    assert tombstones[0] == {"id": "monitor:101", "_deleted": True}
    assert stats2["deleted"] == 1
    assert "monitor:101" not in state["fingerprints"]


def test_deleted_psp_and_alert_contact_emit_tombstones():
    fake = FakeUptimeRobot()
    fake.add_psp("201", "Status Page 1")
    fake.add_alert_contact("301", "Slack Contact")
    state = {}

    _sync(fake, state)
    assert len(state["fingerprints"]) == 2

    del fake.psps["201"]
    del fake.alert_contacts["301"]

    rows2, stats2 = _sync(fake, state)
    tombstone_ids = {r["id"] for r in rows2 if r.get("_deleted")}
    assert tombstone_ids == {"psp:201", "alert_contact:301"}
    assert stats2["deleted"] == 2
    assert "psp:201" not in state["fingerprints"]
    assert "alert_contact:301" not in state["fingerprints"]


def test_deselected_kind_forgets_stored_records():
    fake = FakeUptimeRobot()
    fake.add_monitor("101", "Monitor 1")
    fake.add_alert_contact("301", "Alert 1")
    state = {}

    _sync(fake, state, include=("monitors", "alert_contacts"))
    assert len(state["fingerprints"]) == 2

    # Re-run including only monitors
    rows2, _ = _sync(fake, state, include=("monitors",))
    tombstones = [r for r in rows2 if r.get("_deleted")]
    assert len(tombstones) == 1
    assert tombstones[0]["id"] == "alert_contact:301"
    assert "alert_contact:301" not in state["fingerprints"]


def test_transient_error_during_deletion_check_preserves_record():
    fake = FakeUptimeRobot()
    fake.add_monitor("101", "Monitor 1")
    state = {}

    _sync(fake, state)
    del fake.monitors["101"]

    fake.fail_paths["getMonitors:101"] = UptimeRobotAPIError("Server error on lookup")

    rows2, stats2 = _sync(fake, state)
    assert len(rows2) == 0
    assert stats2["failed"] == 1
    assert "monitor:101" in state["fingerprints"]
