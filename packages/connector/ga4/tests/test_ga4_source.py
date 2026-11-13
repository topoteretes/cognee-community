"""The sync logic, driven with a plain dict standing in for dlt's resource state."""

import pytest
from fake_ga4 import PROPERTY, FakeGA4

from cognee_community_connector_ga4.ga4 import (
    KINDS,
    GA4AccessError,
    GA4APIError,
    GA4AuthError,
    GA4RateLimitedError,
    _iter_rows,
)

BASE = f"properties/{PROPERTY}"
DIMENSION = f"{BASE}/customDimensions/11"
METRIC = f"{BASE}/customMetrics/21"
KEY_EVENT = f"{BASE}/keyEvents/31"
AUDIENCE = f"{BASE}/audiences/41"
ANNOTATION = f"{BASE}/reportingDataAnnotations/51"
ALL_IDS = [DIMENSION, METRIC, KEY_EVENT, AUDIENCE, ANNOTATION]


def _property() -> FakeGA4:
    fake = FakeGA4()
    fake.add_custom_dimension("11", "plan_tier", "Plan tier", scope="USER")
    fake.add_custom_metric("21", "order_margin", "Order margin")
    fake.add_key_event("31", "purchase")
    fake.add_audience("41", "Purchasers", "Users who bought something.")
    fake.add_annotation("51", "Checkout redesign shipped")
    return fake


def _run(fake: FakeGA4, state: dict, **kwargs) -> tuple[list[dict], dict[str, int]]:
    stats: dict[str, int] = {}
    return list(_iter_rows(fake, PROPERTY, state, stats, **kwargs)), stats


def test_a_first_run_emits_every_definition_and_a_quiet_run_emits_nothing():
    fake, state = _property(), {}

    rows, stats = _run(fake, state)
    assert [row["id"] for row in rows] == ALL_IDS
    assert stats == {"scanned": 5, "skipped": 0, "deleted": 0, "failed": 0}
    assert [kind for kind, _ in fake.calls] == list(KINDS)

    rows, stats = _run(fake, state)
    assert rows == []
    assert stats["skipped"] == 5


def test_an_edit_emits_only_the_edited_definition():
    fake, state = _property(), {}
    _run(fake, state)

    fake.records["audiences"]["41"]["membershipDurationDays"] = 90
    rows, _ = _run(fake, state)

    assert [row["id"] for row in rows] == [AUDIENCE]
    assert "Membership duration: 90 days" in rows[0]["content"]


def test_a_definition_gone_from_its_listing_is_tombstoned_once():
    fake, state = _property(), {}
    _run(fake, state)

    del fake.records["custom_dimensions"]["11"]
    del fake.records["annotations"]["51"]
    rows, stats = _run(fake, state)

    assert rows == [{"id": DIMENSION, "_deleted": True}, {"id": ANNOTATION, "_deleted": True}]
    assert stats["deleted"] == 2
    assert _run(fake, state)[0] == []


def test_a_definition_that_comes_back_is_emitted_again():
    fake, state = _property(), {}
    _run(fake, state)
    removed = fake.records["key_events"].pop("31")
    _run(fake, state)

    fake.records["key_events"]["31"] = removed
    rows, _ = _run(fake, state)

    assert [row["id"] for row in rows] == [KEY_EVENT]


def test_deselecting_a_kind_forgets_its_definitions():
    fake, state = _property(), {}
    _run(fake, state)

    rows, _ = _run(fake, state, include=("custom_dimensions", "custom_metrics", "key_events"))

    assert rows == [{"id": AUDIENCE, "_deleted": True}, {"id": ANNOTATION, "_deleted": True}]
    assert ("audiences", PROPERTY) not in fake.calls[-3:]


def test_a_rate_limit_stops_the_run_cleanly_and_deletes_nothing():
    fake, state = _property(), {}
    _run(fake, state)
    del fake.records["audiences"]["41"]
    fake.fail_kinds["key_events"] = GA4RateLimitedError("quota exhausted (HTTP 429)")

    rows, stats = _run(fake, state)

    assert rows == []
    assert stats["failed"] == 1
    assert stats["failed_rate_limit"] == 1
    assert AUDIENCE in state["fingerprints"]

    del fake.fail_kinds["key_events"]
    rows, stats = _run(fake, state)
    assert rows == [{"id": AUDIENCE, "_deleted": True}]
    assert stats["failed"] == 0


def test_a_rate_limited_first_run_keeps_what_it_emitted_and_resumes():
    fake, state = _property(), {}
    fake.fail_kinds["audiences"] = GA4RateLimitedError("quota exhausted (HTTP 429)")

    rows, _ = _run(fake, state)
    assert [row["id"] for row in rows] == [DIMENSION, METRIC, KEY_EVENT]

    del fake.fail_kinds["audiences"]
    rows, _ = _run(fake, state)
    assert [row["id"] for row in rows] == [AUDIENCE, ANNOTATION]


@pytest.mark.parametrize("error", [GA4AuthError("rejected"), GA4AccessError("cannot read")])
def test_bad_credentials_before_any_progress_raise(error):
    fake = _property()
    fake.fail_kinds["custom_dimensions"] = error

    with pytest.raises(type(error)):
        _run(fake, {})


def test_losing_access_after_progress_stops_the_run_and_keeps_the_rest():
    fake, state = _property(), {}
    fake.fail_kinds["audiences"] = GA4AccessError("cannot read")

    rows, stats = _run(fake, state)

    assert [row["id"] for row in rows] == [DIMENSION, METRIC, KEY_EVENT]
    assert stats["failed_access"] == 1


@pytest.mark.parametrize(
    "answer",
    [
        None,
        [{"displayName": "no name"}],
        [{"name": "properties/999/customDimensions/11"}],
        [{"name": f"{BASE}/customDimensions/"}],
        ["not a record"],
    ],
)
def test_a_listing_of_unexpected_shape_raises_and_deletes_nothing(answer):
    fake, state = _property(), {}
    _run(fake, state)
    before = dict(state["fingerprints"])
    fake.overrides["custom_dimensions"] = answer

    with pytest.raises(GA4APIError, match="unexpected shape"):
        _run(fake, state)
    assert state["fingerprints"] == before


def test_an_empty_listing_is_trusted_and_forgets_that_kind():
    fake, state = _property(), {}
    _run(fake, state)
    fake.records["custom_metrics"].clear()

    rows, _ = _run(fake, state)

    assert rows == [{"id": METRIC, "_deleted": True}]


def test_two_properties_sharing_one_state_never_forget_each_other():
    fake, state = _property(), {}
    _run(fake, state)
    other = FakeGA4()

    rows = list(_iter_rows(other, "555", state, {}))

    assert rows == []
    assert set(state["fingerprints"]) == set(ALL_IDS)
