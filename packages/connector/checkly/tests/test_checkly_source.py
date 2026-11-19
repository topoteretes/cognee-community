"""Tests for the Checkly sync walker and row emission contract."""

from fake_checkly import FakeCheckly

from cognee_community_connector_checkly.checkly import (
    ChecklyRateLimitedError,
    _iter_rows,
)

ROW_COLUMNS = {"id", "title", "content", "url", "_deleted"}


def _sync(fake: FakeCheckly, state: dict, **kwargs) -> tuple[list[dict], dict]:
    stats: dict[str, int] = {}
    rows = list(_iter_rows(fake, "https://app.checklyhq.com", state, stats, **kwargs))
    return rows, stats


def _workspace() -> FakeCheckly:
    fake = FakeCheckly()
    fake.add_check_group(
        "g1",
        "Checkout Core",
        tags=["production", "checkout"],
        environmentVariables=[{"key": "STRIPE_SECRET_KEY", "value": "sk_live_verysecret"}],
    )
    fake.add_check(
        "c1",
        "Checkout API Endpoint",
        checkType="API",
        groupId="g1",
        tags=["production", "critical"],
        request={
            "method": "POST",
            "url": "https://api.example.com/checkout",
            "headers": [{"key": "Authorization", "value": "Bearer jwt_secret_credential"}],
        },
    )
    fake.set_check_status("c1", has_failures=False, is_degraded=False)
    fake.add_alert_channel("a1", "Ops PagerDuty", type="PAGERDUTY")
    fake.add_maintenance_window("m1", "Database Backup", tags=["database"])
    return fake


def test_first_sync_emits_every_record_with_frozen_row_shape():
    fake = _workspace()
    rows, stats = _sync(fake, {})

    ids = {r["id"] for r in rows}
    assert ids == {"check_group:g1", "check:c1", "alert_channel:a1", "maintenance_window:m1"}
    assert all(set(r) == ROW_COLUMNS for r in rows)
    assert all(r["_deleted"] is False and r["title"] and r["content"] for r in rows)

    urls = {r["id"]: r["url"] for r in rows}
    assert urls["check:c1"] == "https://app.checklyhq.com/checks/c1"
    assert urls["check_group:g1"] == "https://app.checklyhq.com/checks?groupId=g1"
    assert urls["alert_channel:a1"] == "https://app.checklyhq.com/alerts"
    assert urls["maintenance_window:m1"] == "https://app.checklyhq.com/maintenance-windows"
    assert stats["failed"] == 0


def test_secret_headers_and_env_vars_are_strictly_redacted():
    fake = _workspace()
    rows, _ = _sync(fake, {})

    all_text = " ".join(f"{r['title']} {r['content']}" for r in rows)
    # Secret values must NEVER leak
    assert "sk_live_verysecret" not in all_text
    assert "jwt_secret_credential" not in all_text

    # Configuration keys are preserved for context
    assert "STRIPE_SECRET_KEY" in all_text
    assert "Authorization" in all_text


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


def test_check_status_change_triggers_reemission():
    fake = _workspace()
    state = {}
    _sync(fake, state)

    # Change status of check from HEALTHY to FAILING
    fake.set_check_status("c1", has_failures=True, is_degraded=False)

    rows2, stats2 = _sync(fake, state)
    assert len(rows2) == 1
    assert rows2[0]["id"] == "check:c1"
    assert "FAILING" in rows2[0]["content"]
    assert "FAILING" in rows2[0]["title"]
    assert stats2["skipped"] == 3
    assert stats2["scanned"] == 4


def test_filtering_by_check_tags():
    fake = FakeCheckly()
    fake.add_check("c1", "Prod Check", tags=["production"])
    fake.add_check("c2", "Staging Check", tags=["staging"])

    rows, _ = _sync(fake, {}, check_tags=["production"])
    check_ids = {r["id"] for r in rows if r["id"].startswith("check:")}
    assert check_ids == {"check:c1"}


def test_filtering_by_check_types():
    fake = FakeCheckly()
    fake.add_check("c1", "API Check", checkType="API")
    fake.add_check("c2", "Browser Check", checkType="BROWSER")

    rows, _ = _sync(fake, {}, check_types=["BROWSER"])
    check_ids = {r["id"] for r in rows if r["id"].startswith("check:")}
    assert check_ids == {"check:c2"}


def test_filtering_by_only_active():
    fake = FakeCheckly()
    fake.add_check("c1", "Active Check", activated=True)
    fake.add_check("c2", "Paused Check", activated=False)

    rows, _ = _sync(fake, {}, only_active=True)
    check_ids = {r["id"] for r in rows if r["id"].startswith("check:")}
    assert check_ids == {"check:c1"}


def test_pagination_fetches_multiple_pages():
    fake = FakeCheckly()
    for i in range(130):
        fake.add_check(f"c_{i}", f"Check {i}")

    rows, stats = _sync(fake, {})
    check_ids = {r["id"] for r in rows}
    assert len(check_ids) == 130
    assert stats["scanned"] == 130


def test_request_budget_cutoff():
    fake = FakeCheckly()
    for i in range(10):
        fake.add_check(f"c_{i}", f"Check {i}")

    state = {}
    _, stats = _sync(fake, state, max_requests=1)
    assert stats["failed"] == 1
    assert stats["failed_budget"] == 1


def test_rate_limited_cutoff():
    fake = FakeCheckly()
    fake.add_check("c1", "Check 1")
    fake.fail_paths["/checks"] = ChecklyRateLimitedError("HTTP 429")

    state = {}
    rows, stats = _sync(fake, state)
    assert len(rows) == 0
    assert stats["failed"] == 1
    assert stats["failed_rate_limit"] == 1
