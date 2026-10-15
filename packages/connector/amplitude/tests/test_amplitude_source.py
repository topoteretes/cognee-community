"""The sync walk, driven with a plain dict as dlt resource state."""

import pytest
from fake_amplitude import (
    ANNOTATIONS,
    COHORTS,
    EVENT_PROPERTIES,
    EVENTS,
    UPLOAD_DEFINITION,
    USER_PROPERTIES,
    FakeAmplitude,
)

from cognee_community_connector_amplitude.amplitude import (
    AmplitudeAccessError,
    AmplitudeAPIError,
    AmplitudeAuthError,
    AmplitudeRateLimitedError,
    _iter_rows,
    render_cohort,
)

ROW_COLUMNS = {"id", "title", "content", "url", "_deleted"}
ALL_IDS = {
    "event:Checkout Completed",
    "event:Signup Completed",
    "user_property:gp:plan_tier",
    "cohort:c1",
    "cohort:c2",
    "annotation:1",
}


def _sync(fake: FakeAmplitude, state: dict, **kwargs) -> tuple[list[dict], dict]:
    stats: dict[str, int] = {}
    rows = list(_iter_rows(fake, state, stats, **kwargs))
    return rows, stats


def _ids(rows: list[dict]) -> set[str]:
    return {row["id"] for row in rows}


def _tombstones(rows: list[dict]) -> set[str]:
    return {row["id"] for row in rows if row.get("_deleted")}


def _project() -> FakeAmplitude:
    fake = FakeAmplitude()
    fake.add_event("Checkout Completed", description="A customer paid for an order.")
    fake.add_event_property("Checkout Completed", "cart_value", type="number")
    fake.add_event("Signup Completed")
    fake.add_user_property("gp:plan_tier", description="Plan the account is on.")
    fake.add_user_property("user_id")
    fake.add_cohort("c1", "Paying users")
    fake.add_cohort("c2", "Power users")
    fake.add_annotation(1, "Checkout redesign shipped")
    return fake


def test_first_sync_emits_every_record_with_the_frozen_row_shape():
    rows, stats = _sync(_project(), {})

    assert _ids(rows) == ALL_IDS
    assert all(set(row) == ROW_COLUMNS for row in rows)
    assert all(row["_deleted"] is False and row["title"] and row["content"] for row in rows)
    assert stats == {"scanned": 6, "skipped": 0, "deleted": 0, "failed": 0, "no_access": 0}


def test_an_event_is_rendered_with_its_taxonomy_and_properties():
    fake = FakeAmplitude()
    fake.add_event(
        "Checkout Completed",
        display_name="Order Paid",
        category={"name": "Checkout"},
        description="A customer paid for an order.",
        owner="ada@example.com",
        tags=["revenue", "checkout"],
    )
    fake.add_event_property(
        "Checkout Completed",
        "currency",
        type="enum",
        enum_values=["USD", "EUR"],
        description="ISO currency code.",
    )
    fake.add_event_property("Checkout Completed", "cart_value", type="number", is_required=True)
    fake.add_event_property("Checkout Completed", "coupon")
    fake.add_event_property(
        "Checkout Completed",
        "items",
        type="string",
        is_array_type=True,
        regex="^sku-",
        classifications=["PII"],
    )
    rows, _ = _sync(fake, {}, include=("events",))

    assert rows[0]["id"] == "event:Checkout Completed"
    assert rows[0]["title"] == "Order Paid"
    assert rows[0]["content"].splitlines() == [
        "Event type: Checkout Completed",
        "Category: Checkout",
        "Owner: ada@example.com",
        "Tags: checkout, revenue",
        "",
        "A customer paid for an order.",
        "",
        "Properties:",
        "- cart_value: number; required",
        "- coupon",
        "- currency: enum (EUR, USD). ISO currency code.",
        "- items: string; array; pattern ^sku-; PII",
    ]


def test_an_inactive_event_says_so():
    fake = FakeAmplitude()
    fake.add_event("Legacy Click", is_active=False)
    rows, _ = _sync(fake, {}, include=("events",))

    assert rows[0]["content"].splitlines() == ["Event type: Legacy Click", "Status: inactive"]


def test_user_properties_are_rendered_and_undocumented_built_ins_are_left_out():
    fake = FakeAmplitude()
    fake.add_user_property(
        "gp:plan_tier",
        type="enum",
        enum_values=["team", "free", "pro"],
        description="Plan the account is on.",
    )
    fake.add_user_property("gp:seats")
    fake.add_user_property("user_id")
    fake.add_user_property("device_id", description="Set by the mobile SDK.")
    rows, _ = _sync(fake, {}, include=("user_properties",))
    content = {row["id"]: row["content"] for row in rows}

    assert set(content) == {
        "user_property:gp:plan_tier",
        "user_property:gp:seats",
        "user_property:device_id",
    }
    assert content["user_property:gp:plan_tier"].splitlines() == [
        "User property: plan_tier",
        "Type: enum (free, pro, team)",
        "",
        "Plan the account is on.",
    ]
    assert content["user_property:gp:seats"] == "User property: seats"
    assert {row["title"] for row in rows} == {"plan_tier", "seats", "device_id"}


def test_a_cohort_is_rendered_with_its_definition_and_an_uploaded_one_without():
    fake = FakeAmplitude()
    fake.add_cohort("c1", "Paying users", description="Paid in the last 30 days.")
    fake.add_cohort("c2", "Imported list", type="manual_upload", definition=UPLOAD_DEFINITION)
    rows, _ = _sync(fake, {}, include=("cohorts",))
    content = {row["id"]: row["content"] for row in rows}

    lines = content["cohort:c1"].splitlines()
    assert lines[:5] == [
        "Cohort type: behavioral",
        "Owners: owner@example.com",
        "",
        "Paid in the last 30 days.",
        "",
    ]
    assert lines[5] == "Definition:"
    assert '"type_value": "Checkout Completed"' in lines[6]
    assert '"time_value": 30' in lines[6]
    assert content["cohort:c2"].splitlines() == [
        "Cohort type: uploaded list",
        "Owners: owner@example.com",
    ]


def test_an_annotation_is_rendered_with_its_dates_category_and_chart():
    fake = FakeAmplitude()
    fake.add_annotation(
        1,
        "Pricing experiment",
        end="2026-10-06T00:00:00+00:00",
        details="Annual discount shown to half of new signups.",
        category={"id": 7, "category": "Experiments"},
        chart_id="abc123",
    )
    fake.add_annotation(2, "Release 4.2", category={"id": 8, "name": "Releases"})
    rows, _ = _sync(fake, {}, include=("annotations",))
    content = {row["id"]: row["content"] for row in rows}

    assert content["annotation:1"].splitlines() == [
        "Annotation date: 2026-10-01T00:00:00+00:00 to 2026-10-06T00:00:00+00:00",
        "Category: Experiments",
        "Chart: abc123",
        "",
        "Annual discount shown to half of new signups.",
    ]
    assert content["annotation:2"].splitlines() == [
        "Annotation date: 2026-10-01T00:00:00+00:00",
        "Category: Releases",
    ]


def test_volatile_cohort_fields_are_never_rendered_and_never_trigger_a_resync():
    fake, state = _project(), {}
    rows, _ = _sync(fake, state)
    text = " ".join(f"{row['title']} {row['content']}" for row in rows)

    for volatile in ("4242", "1791474254", "1791474253", "1791474999", "17"):
        assert volatile not in text

    fake.recompute()
    rows, stats = _sync(fake, state)

    assert rows == []
    assert stats["skipped"] == 6


def test_an_unchanged_resync_emits_nothing():
    fake, state = _project(), {}
    _sync(fake, state)

    rows, stats = _sync(fake, state)

    assert rows == []
    assert stats["scanned"] == 6
    assert stats["skipped"] == 6


def test_an_edit_re_emits_only_that_record():
    fake, state = _project(), {}
    _sync(fake, state)

    fake.cohorts["c2"]["description"] = "Used the product on 5 of the last 7 days."
    rows, _ = _sync(fake, state)

    assert _ids(rows) == {"cohort:c2"}
    assert "Used the product on 5 of the last 7 days." in rows[0]["content"]


def test_a_property_edit_re_emits_only_its_event():
    fake, state = _project(), {}
    _sync(fake, state)

    fake.event_properties["Checkout Completed"]["cart_value"]["description"] = "Order total."
    rows, _ = _sync(fake, state)

    assert _ids(rows) == {"event:Checkout Completed"}
    assert "- cart_value: number. Order total." in rows[0]["content"]


def test_a_deletion_of_each_kind_is_tombstoned():
    fake, state = _project(), {}
    _sync(fake, state)

    del fake.events["Signup Completed"]
    del fake.user_properties["gp:plan_tier"]
    del fake.cohorts["c2"]
    del fake.annotations[1]
    rows, stats = _sync(fake, state)

    assert rows == [
        {"id": "event:Signup Completed", "_deleted": True},
        {"id": "user_property:gp:plan_tier", "_deleted": True},
        {"id": "cohort:c2", "_deleted": True},
        {"id": "annotation:1", "_deleted": True},
    ]
    assert stats["deleted"] == 4
    assert set(state["fingerprints"]) == {"event:Checkout Completed", "cohort:c1"}


def test_a_renamed_event_replaces_the_old_document():
    fake, state = _project(), {}
    _sync(fake, state)

    fake.events["Signup Finished"] = {
        **fake.events.pop("Signup Completed"),
        "event_type": "Signup Finished",
    }
    rows, _ = _sync(fake, state)

    assert _tombstones(rows) == {"event:Signup Completed"}
    assert _ids(rows) - _tombstones(rows) == {"event:Signup Finished"}


def test_an_archived_cohort_is_forgotten_unless_archived_ones_are_included():
    fake, state = _project(), {}
    _sync(fake, state)

    fake.cohorts["c2"]["archived"] = True
    rows, _ = _sync(fake, state)
    assert rows == [{"id": "cohort:c2", "_deleted": True}]

    rows, _ = _sync(fake, state, include_archived=True)
    assert _ids(rows) == {"cohort:c2"}
    assert "Status: archived" in rows[0]["content"]


def test_deselecting_a_kind_forgets_its_records_without_reading_it():
    fake, state = _project(), {}
    _sync(fake, state)

    rows, _ = _sync(fake, state, include=("events", "annotations"))

    assert _tombstones(rows) == {"user_property:gp:plan_tier", "cohort:c1", "cohort:c2"}
    assert fake.count(COHORTS) == 1
    assert fake.count(USER_PROPERTIES) == 1


def test_a_kind_the_plan_cannot_read_is_skipped_and_keeps_its_documents():
    fake, state = _project(), {}
    _sync(fake, state)

    del fake.cohorts["c2"]
    fake.fail_paths[EVENTS] = AmplitudeAccessError("The Amplitude plan cannot call it (HTTP 403)")
    rows, stats = _sync(fake, state)

    assert rows == [{"id": "cohort:c2", "_deleted": True}]
    assert stats["no_access"] == 1
    assert stats["failed"] == 0
    assert "event:Checkout Completed" in state["fingerprints"]

    del fake.fail_paths[EVENTS]
    rows, stats = _sync(fake, state)

    assert rows == []
    assert stats["no_access"] == 0


def test_a_first_sync_without_taxonomy_access_still_syncs_the_rest():
    fake = _project()
    fake.fail_paths[EVENTS] = AmplitudeAccessError("no access (HTTP 403)")
    fake.fail_paths[USER_PROPERTIES] = AmplitudeAccessError("no access (HTTP 403)")

    rows, stats = _sync(fake, {})

    assert _ids(rows) == {"cohort:c1", "cohort:c2", "annotation:1"}
    assert stats["no_access"] == 2


def test_a_rate_limited_run_stops_cleanly_and_resumes_without_false_deletes():
    fake, state = _project(), {}
    _sync(fake, state)
    del fake.cohorts["c2"]
    del fake.annotations[1]

    fake.fail_paths[COHORTS] = AmplitudeRateLimitedError("Amplitude rate limit exceeded (HTTP 429)")
    rows, stats = _sync(fake, state)

    assert rows == []
    assert stats["failed"] == 1
    assert stats["failed_rate_limit"] == 1
    assert {"cohort:c2", "annotation:1"} <= set(state["fingerprints"])
    assert fake.count(ANNOTATIONS) == 1  # the run stopped before the next kind

    del fake.fail_paths[COHORTS]
    rows, stats = _sync(fake, state)

    assert _tombstones(rows) == {"cohort:c2", "annotation:1"}
    assert stats["failed"] == 0


def test_a_budget_cut_among_events_resumes_after_the_events_already_rendered():
    fake, state = FakeAmplitude(), {}
    for n in range(6):
        fake.add_event(f"Event {n}")
        fake.add_event_property(f"Event {n}", "source")
    rows, stats = _sync(fake, state, include=("events",), max_requests=4)

    assert stats["failed_budget"] == 1
    assert _ids(rows) == {"event:Event 0", "event:Event 1", "event:Event 2"}
    assert state["events_rendered"] == ["event:Event 0", "event:Event 1", "event:Event 2"]

    del fake.events["Event 1"]
    rows, stats = _sync(fake, state, include=("events",))

    assert stats["failed"] == 0
    assert _ids(rows) - _tombstones(rows) == {"event:Event 3", "event:Event 4", "event:Event 5"}
    assert _tombstones(rows) == {"event:Event 1"}
    assert fake.count(EVENT_PROPERTIES) == 6  # no event's properties were read twice
    assert state["events_rendered"] == []


def test_an_auth_error_before_any_row_raises():
    fake = _project()
    fake.fail_after = 0
    fake.fail_with = AmplitudeAuthError("Amplitude rejected the API key or secret key (HTTP 403)")

    with pytest.raises(AmplitudeAuthError):
        _sync(fake, {})


def test_an_auth_error_after_rows_stops_the_run_and_keeps_them():
    fake, state = _project(), {}
    fake.fail_paths[COHORTS] = AmplitudeAuthError("Amplitude rejected the secret key (HTTP 403)")

    rows, stats = _sync(fake, state)

    assert _ids(rows) == {
        "event:Checkout Completed",
        "event:Signup Completed",
        "user_property:gp:plan_tier",
    }
    assert stats["failed_auth"] == 1


@pytest.mark.parametrize(
    "broken",
    [
        {"success": False, "errors": [{"message": "boom"}]},
        {"success": True},
        {"success": True, "data": None},
        {"success": True, "data": {"event_type": "Checkout Completed"}},
        {"success": True, "data": ["Checkout Completed"]},
        {"success": True, "data": [{"description": "an event without its type"}]},
    ],
)
def test_a_malformed_listing_aborts_instead_of_forgetting(broken):
    fake, state = _project(), {}
    _sync(fake, state)

    fake.overrides[EVENTS] = broken
    with pytest.raises(AmplitudeAPIError):
        _sync(fake, state)

    assert {"event:Checkout Completed", "event:Signup Completed"} <= set(state["fingerprints"])


def test_a_listing_that_is_really_empty_forgets_the_kind():
    fake, state = _project(), {}
    _sync(fake, state)

    fake.annotations.clear()
    rows, _ = _sync(fake, state)

    assert rows == [{"id": "annotation:1", "_deleted": True}]


def test_an_unknown_cohort_type_is_rendered_as_amplitude_names_it():
    row = render_cohort({"id": "c9", "name": "Likely to churn", "type": "prediction"})

    assert row["content"] == "Cohort type: prediction"


def test_a_named_chart_is_rendered_from_its_export_header_only():
    fake = FakeAmplitude()
    fake.add_chart("ch1", "Checkouts, per day", "Uniques", "Order Paid")
    rows, stats = _sync(fake, {}, include=("cohorts",), chart_ids=("ch1",))

    assert rows == [
        {
            "id": "chart:ch1",
            "title": "Checkouts, per day",
            "content": "Chart: ch1\n\nDefinition:\nUniques\nOrder Paid",
            "url": "",
            "_deleted": False,
        }
    ]
    assert stats["scanned"] == 1


def test_chart_results_are_never_rendered_and_never_trigger_a_resync():
    fake, state = FakeAmplitude(), {}
    fake.add_chart("ch1", "Checkouts per day", "Uniques", "Order Paid")
    rows, _ = _sync(fake, state, chart_ids=("ch1",))

    for volatile in ("7001", "2026-10-01", "All Users", "Segment"):
        assert volatile not in rows[0]["content"]

    fake.recompute()
    rows, stats = _sync(fake, state, chart_ids=("ch1",))

    assert rows == []
    assert stats["skipped"] == 1


def test_charts_are_only_read_when_their_ids_are_named():
    fake = _project()
    fake.add_chart("ch1", "Checkouts per day")

    rows, _ = _sync(fake, {})

    assert _ids(rows) == ALL_IDS
    assert fake.count("/api/3/chart/ch1/csv") == 0


def test_a_chart_deleted_in_amplitude_or_no_longer_named_is_forgotten():
    fake, state = FakeAmplitude(), {}
    for chart_id in ("ch1", "ch2", "ch3"):
        fake.add_chart(chart_id, f"Chart {chart_id}")
    _sync(fake, state, chart_ids=("ch1", "ch2", "ch3"))

    del fake.charts["ch2"]
    rows, stats = _sync(fake, state, chart_ids=("ch1", "ch2"))

    assert rows == [{"id": "chart:ch2", "_deleted": True}, {"id": "chart:ch3", "_deleted": True}]
    assert stats["deleted"] == 2

    rows, _ = _sync(fake, state)
    assert rows == [{"id": "chart:ch1", "_deleted": True}]


def test_a_chart_id_that_never_existed_is_left_out_quietly():
    fake = FakeAmplitude()

    rows, stats = _sync(fake, {}, include=("cohorts",), chart_ids=("typo",))

    assert rows == []
    assert stats["deleted"] == 0
    assert stats["failed"] == 0


def test_a_rate_limit_among_charts_resumes_after_the_charts_already_rendered():
    fake, state = FakeAmplitude(), {}
    for chart_id in ("ch1", "ch2", "ch3"):
        fake.add_chart(chart_id, f"Chart {chart_id}")
    _sync(fake, state, include=("cohorts",), chart_ids=("ch1", "ch2", "ch3"))
    del fake.charts["ch3"]

    limited = AmplitudeRateLimitedError("Amplitude rate limit exceeded (HTTP 429)")
    fake.fail_paths["/api/3/chart/ch2/csv"] = limited
    rows, stats = _sync(fake, state, include=("cohorts",), chart_ids=("ch1", "ch2", "ch3"))

    assert rows == []
    assert stats["failed_rate_limit"] == 1
    assert state["charts_rendered"] == ["chart:ch1"]

    del fake.fail_paths["/api/3/chart/ch2/csv"]
    rows, stats = _sync(fake, state, include=("cohorts",), chart_ids=("ch1", "ch2", "ch3"))

    assert rows == [{"id": "chart:ch3", "_deleted": True}]
    assert fake.count("/api/3/chart/ch1/csv") == 2  # not read again after the cut
    assert stats["failed"] == 0


@pytest.mark.parametrize("broken", [{}, {"data": None}, {"data": {"series": []}}])
def test_a_malformed_chart_export_aborts(broken):
    fake = FakeAmplitude()
    fake.overrides["/api/3/chart/ch1/csv"] = broken

    with pytest.raises(AmplitudeAPIError):
        _sync(fake, {}, include=("cohorts",), chart_ids=("ch1",))
