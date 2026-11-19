"""Tests for the UptimeRobot sync walker and row emission contract."""

from fake_uptimerobot import FakeUptimeRobot

from cognee_community_connector_uptimerobot.uptimerobot import (
    UptimeRobotRateLimitedError,
    _iter_rows,
)

ROW_COLUMNS = {"id", "title", "content", "url", "_deleted"}


def _sync(fake: FakeUptimeRobot, state: dict, **kwargs) -> tuple[list[dict], dict]:
    stats: dict[str, int] = {}
    rows = list(_iter_rows(fake, "https://uptimerobot.com/dashboard", state, stats, **kwargs))
    return rows, stats


def _workspace() -> FakeUptimeRobot:
    fake = FakeUptimeRobot()
    fake.add_monitor(
        "101",
        "Primary Production API",
        type=1,
        status=2,  # UP
        url="https://api.example.com/v1/health",
        http_username="admin",
        http_password="supersecretpassword",
    )
    fake.add_psp("201", "Global System Status", monitors=[101])
    fake.add_alert_contact(
        "301", "OnCall Slack", type=11, value="https://hooks.slack.com/services/secret"
    )
    fake.add_mwindow("401", "Sunday Maintenance", duration=60)
    return fake


def test_first_sync_emits_every_record_with_frozen_row_shape():
    fake = _workspace()
    rows, stats = _sync(fake, {})

    ids = {r["id"] for r in rows}
    assert ids == {"monitor:101", "psp:201", "alert_contact:301", "mwindow:401"}
    assert all(set(r) == ROW_COLUMNS for r in rows)
    assert all(r["_deleted"] is False and r["title"] and r["content"] for r in rows)

    urls = {r["id"]: r["url"] for r in rows}
    assert urls["monitor:101"] == "https://uptimerobot.com/dashboard#monitor=101"
    assert urls["psp:201"] == "https://status.example.com"
    assert urls["alert_contact:301"] == "https://uptimerobot.com/dashboard#mySettings"
    assert urls["mwindow:401"] == "https://uptimerobot.com/dashboard#mySettings"
    assert stats["failed"] == 0


def test_credentials_and_webhooks_are_strictly_redacted():
    fake = _workspace()
    rows, _ = _sync(fake, {})

    all_text = " ".join(f"{r['title']} {r['content']}" for r in rows)
    assert "supersecretpassword" not in all_text
    assert "hooks.slack.com/services/secret" not in all_text
    assert "[Redacted & Protected]" in all_text
    assert "[Configured & Protected]" in all_text


def test_second_sync_skips_unchanged_records():
    fake = _workspace()
    state = {}
    rows1, stats1 = _sync(fake, state)
    assert len(rows1) == 4
    assert stats1["scanned"] == 4
    assert stats1["skipped"] == 0

    rows2, stats2 = _sync(fake, state)
    assert len(rows2) == 0
    assert stats2["scanned"] == 4
    assert stats2["skipped"] == 4


def test_monitor_status_transition_triggers_reemission():
    fake = _workspace()
    state = {}
    _sync(fake, state)

    # Change monitor status from UP (2) to DOWN (9)
    fake.monitors["101"]["status"] = 9
    fake.monitors["101"]["average_response_time"] = "0.00"

    rows2, stats2 = _sync(fake, state)
    assert len(rows2) == 1
    assert rows2[0]["id"] == "monitor:101"
    assert "DOWN" in rows2[0]["content"]
    assert "DOWN" in rows2[0]["title"]
    assert stats2["skipped"] == 3
    assert stats2["scanned"] == 4


def test_filtering_by_monitor_types():
    fake = FakeUptimeRobot()
    fake.add_monitor("1", "HTTP Check", type=1)
    fake.add_monitor("2", "Ping Check", type=3)

    rows, _ = _sync(fake, {}, monitor_types=["Ping"])
    mon_ids = {r["id"] for r in rows if r["id"].startswith("monitor:")}
    assert mon_ids == {"monitor:2"}


def test_filtering_by_only_down():
    fake = FakeUptimeRobot()
    fake.add_monitor("1", "Healthy Site", status=2)
    fake.add_monitor("2", "Crashed Site", status=9)

    rows, _ = _sync(fake, {}, only_down=True)
    mon_ids = {r["id"] for r in rows if r["id"].startswith("monitor:")}
    assert mon_ids == {"monitor:2"}


def test_pagination_fetches_multiple_pages():
    fake = FakeUptimeRobot()
    for i in range(120):
        fake.add_monitor(str(i + 1), f"Monitor {i + 1}")

    rows, stats = _sync(fake, {})
    mon_ids = {r["id"] for r in rows}
    assert len(mon_ids) == 120
    assert stats["scanned"] == 120


def test_request_budget_cutoff():
    fake = FakeUptimeRobot()
    for i in range(10):
        fake.add_monitor(str(i + 1), f"Monitor {i + 1}")

    state = {}
    _, stats = _sync(fake, state, max_requests=1)
    assert stats["failed"] == 1
    assert stats["failed_budget"] == 1


def test_rate_limited_cutoff():
    fake = FakeUptimeRobot()
    fake.add_monitor("1", "Monitor 1")
    fake.fail_paths["getMonitors"] = UptimeRobotRateLimitedError("HTTP 429")

    state = {}
    rows, stats = _sync(fake, state)
    assert len(rows) == 0
    assert stats["failed"] == 1
    assert stats["failed_rate_limit"] == 1
