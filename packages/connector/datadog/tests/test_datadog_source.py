"""The sync itself: rendering, the incremental cycle, forget-on-delete and the stops.

``_iter_rows`` is pure of dlt, so these tests drive it with a dict as the state and
the fake API behind the real client.
"""

import pytest
from fake_datadog import FakeDatadog, client_for

from cognee_community_connector_datadog.datadog import (
    KINDS,
    DatadogAccessError,
    DatadogAuthError,
    DatadogSourceError,
    _iter_rows,
)


def _org() -> FakeDatadog:
    fake = FakeDatadog()
    fake.add_monitor(1, "Checkout latency", "Runbook: roll back payments-api.")
    fake.add_monitor(2, "Disk full", "Page the infra on-call.")
    fake.add_dashboard(
        "abc-123",
        "Checkout",
        [
            {"id": 1, "definition": {"type": "note", "content": "Owned by payments."}},
            {
                "id": 2,
                "definition": {
                    "type": "group",
                    "title": "Latency",
                    "widgets": [
                        {"id": 3, "definition": {"type": "timeseries", "title": "p99 latency"}}
                    ],
                },
            },
        ],
    )
    fake.add_notebook(10, "Postmortem: checkout outage", "The deploy skipped the canary.")
    fake.add_slo("slo1", "Checkout availability", monitor_ids=[1])
    fake.add_incident("inc-1", 7, "Checkout down in EU")
    return fake


def _run(fake, state, include=KINDS, **kwargs):
    stats: dict[str, int] = {}
    client = kwargs.pop("client", None) or client_for(fake)
    client.rate_limit = {}
    rows = list(_iter_rows(client, state, stats, include=include, **kwargs))
    return {row["id"]: row for row in rows}, stats


def test_a_first_sync_renders_every_kind_from_its_human_written_fields():
    rows, stats = _run(_org(), {})

    assert set(rows) == {
        "monitor:1",
        "monitor:2",
        "dashboard:abc-123",
        "notebook:10",
        "slo:slo1",
        "incident:inc-1",
    }
    assert stats["deleted"] == 0
    assert stats["failed"] == 0
    monitor = rows["monitor:1"]
    assert monitor["title"] == "Checkout latency"
    assert "Runbook: roll back payments-api." in monitor["content"]
    assert "Thresholds: critical 90, warning 80" in monitor["content"]
    assert "OK" not in monitor["content"], "the live state must stay out of the document"
    assert monitor["url"] == "https://app.datadoghq.com/monitors/1"
    dashboard = rows["dashboard:abc-123"]["content"]
    assert "Owned by payments." in dashboard
    assert "- Latency" in dashboard and "- p99 latency" in dashboard
    assert (
        rows["dashboard:abc-123"]["url"] == "https://app.datadoghq.com/dashboard/abc-123/checkout"
    )
    notebook = rows["notebook:10"]["content"]
    assert "Type: postmortem" in notebook
    assert "The deploy skipped the canary." in notebook
    assert "Monitors: Checkout latency" in rows["slo:slo1"]["content"]
    assert "Targets: 99.9% over 30d" in rows["slo:slo1"]["content"]
    incident = rows["incident:inc-1"]
    assert incident["title"] == "Incident #7: Checkout down in EU"
    for line in (
        "Severity: SEV-2",
        "State: resolved",
        "Customer impact: yes, Checkout failed for EU users",
        "Declared: 2026-10-02",
        "Root cause: A bad deploy of payments-api.",
        "Services: payments-api",
    ):
        assert line in incident["content"]
    assert "Teams" not in incident["content"], "an empty field is left out"


def test_a_quiet_second_sync_emits_nothing_and_fetches_no_details():
    fake = _org()
    state: dict = {}
    _run(fake, state)
    fake.calls.clear()

    rows, stats = _run(fake, state)

    assert rows == {}
    assert stats["skipped"] == 6
    assert not [c for c in fake.calls if c.startswith("GET /api/v1/dashboard/")]
    assert not [c for c in fake.calls if c.startswith("GET /api/v1/notebooks/")]


def test_only_what_changed_is_emitted_again():
    fake = _org()
    state: dict = {}
    _run(fake, state)

    fake.monitors["1"]["message"] = "Runbook: roll back, then page payments."
    fake.monitors["2"]["overall_state"] = "Alert"  # live state, not content
    fake.dashboards["abc-123"]["widgets"].append(
        {"id": 4, "definition": {"type": "note", "content": "Freeze on Fridays."}}
    )
    fake.dashboards["abc-123"]["modified_at"] = fake.stamp()
    fake.incidents["inc-1"]["attributes"]["fields"]["summary"]["value"] = "Payments timed out."
    rows, _ = _run(fake, state)

    assert set(rows) == {"monitor:1", "dashboard:abc-123", "incident:inc-1"}
    assert "Freeze on Fridays." in rows["dashboard:abc-123"]["content"]


def test_a_dashboard_saved_without_a_rendered_change_is_not_emitted_or_fetched_twice():
    fake = _org()
    state: dict = {}
    _run(fake, state)

    fake.dashboards["abc-123"]["modified_at"] = fake.stamp()
    rows, _ = _run(fake, state)
    assert rows == {}

    fake.calls.clear()
    _run(fake, state)
    assert not [c for c in fake.calls if c.startswith("GET /api/v1/dashboard/")]


def test_a_renamed_monitor_renames_the_slo_built_on_it():
    fake = _org()
    state: dict = {}
    _run(fake, state)

    fake.monitors["1"]["name"] = "Checkout p99 latency"
    rows, _ = _run(fake, state)

    assert set(rows) == {"monitor:1", "slo:slo1"}
    assert "Monitors: Checkout p99 latency" in rows["slo:slo1"]["content"]


def test_hard_deleted_items_are_forgotten_after_a_check():
    fake = _org()
    state: dict = {}
    _run(fake, state)

    del fake.monitors["2"]
    del fake.notebooks["10"]
    del fake.slos["slo1"]
    rows, stats = _run(fake, state)

    assert rows == {
        "monitor:2": {"id": "monitor:2", "_deleted": True},
        "notebook:10": {"id": "notebook:10", "_deleted": True},
        "slo:slo1": {"id": "slo:slo1", "_deleted": True},
    }
    assert stats["deleted"] == 3
    assert "GET /api/v1/monitor/2" in fake.calls

    rows, _ = _run(fake, state)
    assert rows == {}, "a forgotten item is not forgotten twice"


def test_an_item_the_listing_skipped_but_datadog_still_has_is_kept():
    fake = _org()
    state: dict = {}
    _run(fake, state)

    fake.hidden.add("2")
    rows, stats = _run(fake, state)

    assert rows == {}
    assert stats["deleted"] == 0
    assert "GET /api/v1/monitor/2" in fake.calls


def test_a_trashed_dashboard_is_forgotten_and_comes_back_when_restored():
    fake = _org()
    state: dict = {}
    _run(fake, state)

    fake.trash_dashboard("abc-123")
    rows, _ = _run(fake, state)
    assert rows == {"dashboard:abc-123": {"id": "dashboard:abc-123", "_deleted": True}}
    assert "GET /api/v1/dashboard/abc-123" in fake.calls, "the trash is confirmed by a 404"

    # a restore keeps the old modified_at, so only the forgotten state brings it back
    fake.restore_dashboard("abc-123")
    rows, _ = _run(fake, state)
    assert set(rows) == {"dashboard:abc-123"}
    assert rows["dashboard:abc-123"]["_deleted"] is False


def test_a_notebook_turned_into_a_template_is_forgotten():
    fake = _org()
    state: dict = {}
    _run(fake, state)

    fake.notebooks["10"]["attributes"]["metadata"]["is_template"] = True
    rows, _ = _run(fake, state)

    assert rows == {"notebook:10": {"id": "notebook:10", "_deleted": True}}


def test_test_incidents_are_left_out_and_one_marked_later_is_forgotten():
    fake = _org()
    fake.add_incident("inc-2", 8, "Game day drill")["attributes"]["is_test"] = True
    state: dict = {}
    rows, _ = _run(fake, state)
    assert "incident:inc-2" not in rows

    fake.incidents["inc-1"]["attributes"]["is_test"] = True
    rows, _ = _run(fake, state)
    assert rows == {"incident:inc-1": {"id": "incident:inc-1", "_deleted": True}}


def test_resolved_incidents_stay():
    fake = _org()
    state: dict = {}
    _run(fake, state)

    fake.incidents["inc-1"]["attributes"]["resolved"] = "2026-10-02T09:30:00.000000+00:00"
    rows, _ = _run(fake, state)

    assert rows["incident:inc-1"]["_deleted"] is False
    assert "Resolved: 2026-10-02" in rows["incident:inc-1"]["content"]


def test_an_org_custom_field_and_the_deprecated_severity_are_rendered():
    fake = FakeDatadog()
    incident = fake.add_incident("inc-1", 7, "Checkout down")
    attributes = incident["attributes"]
    del attributes["fields"]["severity"]
    attributes["severity"] = "SEV-1"
    attributes["fields"]["detection_method"] = {"type": "dropdown", "value": "monitor"}
    attributes["fields"]["affected_region"] = {"type": "textbox", "value": "eu-west-1"}

    rows, _ = _run(fake, {}, include=("incidents",))

    content = rows["incident:inc-1"]["content"]
    assert "Severity: SEV-1" in content
    assert "Detection method: monitor" in content
    assert "Affected region: eu-west-1" in content


def test_every_page_of_a_long_list_is_read():
    fake = FakeDatadog()
    for i in range(1, 251):
        fake.add_monitor(i, f"Monitor {i}")
    for i in range(1, 151):
        fake.add_notebook(i, f"Notebook {i}", "text")

    rows, _ = _run(fake, {}, include=("monitors", "notebooks"))

    assert len(rows) == 400
    monitor_pages = [c for c in fake.calls if c.startswith("GET /api/v1/monitor?")]
    assert [c.split("page=")[-1].split("&")[0] for c in monitor_pages] == ["0", "1", "2", "3"]
    notebook_pages = [c for c in fake.calls if c.startswith("GET /api/v1/notebooks?")]
    assert len(notebook_pages) == 2, "the total count ends the notebook listing"


def test_a_spent_rate_limit_window_stops_only_its_own_kind():
    fake = _org()
    state: dict = {}
    _run(fake, state)

    fake.trash_dashboard("abc-123")
    del fake.monitors["2"]
    fake.remaining["list:dashboards"] = 1
    client = client_for(fake)
    stats: dict[str, int] = {}
    # the window is seen on the first answer, so a two-page listing stops before page two
    for i in range(200):
        fake.add_dashboard(f"d-{i:03}", f"Board {i}")
    rows = {r["id"]: r for r in _iter_rows(client, state, stats)}

    assert "dashboard:abc-123" not in rows, "an unfinished listing must not forget anything"
    assert rows["monitor:2"]["_deleted"] is True, "the other kinds go on"
    assert stats["failed_rate_limit_dashboards"] == 1


def test_a_429_on_a_list_stops_that_kind_and_the_next_run_finishes_it():
    fake = _org()
    state: dict = {}
    fake.statuses["list:notebooks"] = 429
    rows, stats = _run(fake, state)
    assert "notebook:10" not in rows
    assert stats["failed_rate_limit_notebooks"] == 1
    assert "monitor:1" in rows

    del fake.statuses["list:notebooks"]
    rows, _ = _run(fake, state)
    assert set(rows) == {"notebook:10"}


def test_a_spent_detail_window_defers_the_fetch_but_still_forgets():
    fake = _org()
    fake.add_dashboard("def-456", "Payments")
    state: dict = {}
    _run(fake, state)

    for dashboard_id in ("abc-123", "def-456"):
        fake.dashboards[dashboard_id]["description"] = "edited"
        fake.dashboards[dashboard_id]["modified_at"] = fake.stamp()
    fake.add_dashboard("ghi-789", "New board")
    fake.statuses["get:dashboards"] = 429
    del fake.notebooks["10"]
    rows, stats = _run(fake, state)

    assert not [r for r in rows if r.startswith("dashboard:")]
    assert stats["deferred"] == 3
    assert rows["notebook:10"]["_deleted"] is True

    del fake.statuses["get:dashboards"]
    rows, _ = _run(fake, state)
    assert set(rows) == {"dashboard:abc-123", "dashboard:def-456", "dashboard:ghi-789"}


def test_a_run_out_of_budget_keeps_its_state_and_later_runs_finish():
    fake = FakeDatadog()
    for i in range(1, 6):
        fake.add_dashboard(f"d-{i}", f"Board {i}")
    state: dict = {}
    rows, stats = _run(fake, state, include=("dashboards",), max_requests=3)
    assert stats["failed_budget"] == 1
    seen = set(rows)
    for _ in range(3):
        rows, _ = _run(fake, state, include=("dashboards",), max_requests=3)
        assert all(not row["_deleted"] for row in rows.values())
        seen |= set(rows)
    assert seen == {f"dashboard:d-{i}" for i in range(1, 6)}


def test_a_kind_the_keys_cannot_read_is_skipped():
    fake = _org()
    fake.statuses["list:incidents"] = 403
    rows, stats = _run(fake, {})

    assert "incident:inc-1" not in rows
    assert "monitor:1" in rows
    assert stats["failed_access_incidents"] == 1


def test_a_server_error_on_one_listing_skips_that_kind_without_forgetting_it():
    fake = _org()
    state: dict = {}
    _run(fake, state)

    fake.statuses["list:slos"] = 500
    del fake.monitors["2"]
    rows, stats = _run(fake, state)

    assert rows == {"monitor:2": {"id": "monitor:2", "_deleted": True}}
    assert stats["failed_error_slos"] == 1
    assert "slo:slo1" in state["rows"], "a failed listing proves nothing deleted"
    assert not [c for c in fake.calls if c.startswith("GET /api/v1/slo/")], (
        "nothing of a failed listing is even re-checked"
    )


def test_a_key_revoked_mid_run_keeps_the_rows_already_read():
    fake = _org()
    fake.statuses["list:notebooks"] = 401
    state: dict = {}
    rows, stats = _run(fake, state)

    assert {"monitor:1", "monitor:2", "dashboard:abc-123"} <= set(rows)
    assert "notebook:10" not in rows
    assert stats["failed_auth"] == 1
    assert set(state["rows"]) == set(rows), "the state matches what was emitted"

    del fake.statuses["list:notebooks"]
    rows, _ = _run(fake, state)
    assert set(rows) == {"notebook:10", "slo:slo1", "incident:inc-1"}, "the next run finishes"


def test_a_key_revoked_before_anything_is_read_fails_loudly():
    fake = _org()
    state: dict = {}
    _run(fake, state)

    # a quiet run emits nothing before the 401, so there is nothing to keep
    fake.statuses["list:notebooks"] = 401
    with pytest.raises(DatadogAuthError):
        _run(fake, state)


def test_a_rejected_api_key_fails_the_run():
    fake = _org()
    fake.statuses["list:monitors"] = 401

    with pytest.raises(DatadogAuthError):
        _run(fake, {})


def test_keys_that_can_read_nothing_fail_the_run():
    fake = _org()
    for kind in KINDS:
        fake.statuses[f"list:{kind}"] = 403

    with pytest.raises(DatadogAccessError, match="every selected kind"):
        _run(fake, {})


def test_deselecting_a_kind_forgets_its_rows():
    fake = _org()
    state: dict = {}
    _run(fake, state)

    rows, _ = _run(fake, state, include=("monitors", "dashboards", "notebooks", "slos"))

    assert rows == {"incident:inc-1": {"id": "incident:inc-1", "_deleted": True}}


def test_state_written_for_another_site_is_refused():
    fake = _org()
    state: dict = {}
    _run(fake, state)

    eu = FakeDatadog(site="datadoghq.eu")
    with pytest.raises(DatadogSourceError, match="another Datadog site"):
        _run(eu, state, client=client_for(eu, site="datadoghq.eu"))
