"""The sync walk, driven with a plain dict as dlt resource state."""

import random

import pytest
from fake_apollo import FakeApollo

from cognee_community_connector_apollo.apollo import (
    PAGE_SIZE,
    ApolloAccessError,
    ApolloAPIError,
    ApolloAuthError,
    ApolloRateLimitedError,
    ApolloSourceError,
    Lookups,
    _iter_rows,
    render_contact,
)

ROW_COLUMNS = {"id", "title", "content", "url", "_deleted"}


def _sync(fake: FakeApollo, state: dict, **kwargs) -> tuple[list[dict], dict]:
    stats: dict[str, int] = {}
    rows = list(_iter_rows(fake, state, stats, **kwargs))
    return rows, stats


def _ids(rows: list[dict]) -> set[str]:
    return {row["id"] for row in rows}


def _tombstones(rows: list[dict]) -> set[str]:
    return {row["id"] for row in rows if row.get("_deleted")}


def _workspace() -> FakeApollo:
    fake = FakeApollo()
    fake.add_sequence("s1", "Q4 Outreach")
    fake.add_account("a1", "Acme")
    fake.add_account("a2", "Globex")
    fake.add_contact("c1", "Ada Lovelace", account_id="a1")
    fake.add_contact("c2", "Ben Turing", account_id="a2")
    return fake


def test_first_sync_emits_every_record_with_the_frozen_row_shape():
    rows, stats = _sync(_workspace(), {})

    assert _ids(rows) == {"sequence:s1", "account:a1", "account:a2", "contact:c1", "contact:c2"}
    assert all(set(row) == ROW_COLUMNS for row in rows)
    assert all(row["_deleted"] is False and row["title"] and row["content"] for row in rows)
    urls = {row["id"]: row["url"] for row in rows}
    assert urls["contact:c1"] == "https://app.apollo.io/#/contacts/c1"
    assert urls["account:a1"] == "https://app.apollo.io/#/accounts/a1"
    assert urls["sequence:s1"] == "https://app.apollo.io/#/sequences/s1"
    assert stats["failed"] == 0


def test_enrichment_channels_and_volatile_stats_are_never_rendered():
    rows, _ = _sync(_workspace(), {})
    text = " ".join(f"{row['title']} {row['content']}" for row in rows)

    for leaked in ("example.com/in", "linkedin", "Enriched", "555 0100", "@example.com", "5000"):
        assert leaked not in text
    assert "0.4" not in text  # sequence open rate


def test_stage_list_and_custom_field_ids_are_rendered_as_names():
    fake = FakeApollo()
    fake.add_contact(
        "c1",
        "Ada Lovelace",
        title="CTO",
        contact_stage_id="cs2",
        label_ids=["l1"],
        typed_custom_fields={"f1": "Lead with data quality"},
    )
    fake.add_account(
        "a1", "Acme", account_stage_id="as2", label_ids=["l2"], typed_custom_fields={"f2": "Gold"}
    )
    rows, _ = _sync(fake, {}, include=("contacts", "accounts"))
    content = {row["id"]: row["content"] for row in rows}

    assert content["contact:c1"].splitlines() == [
        "Job title: CTO",
        "Company: Acme",
        "Stage: Interested",
        "Lists: Champions",
        "Messaging Angle: Lead with data quality",
    ]
    assert content["account:a1"].splitlines() == [
        "Domain: a1.example.com",
        "Stage: Customer",
        "Lists: Key Accounts",
        "Tier: Gold",
    ]


def test_an_unchanged_resync_emits_nothing():
    fake, state = _workspace(), {}
    _sync(fake, state)

    rows, stats = _sync(fake, state)

    assert rows == []
    assert stats["scanned"] == 5
    assert stats["skipped"] == 5


def test_an_edit_re_emits_only_that_record():
    fake, state = _workspace(), {}
    _sync(fake, state)

    fake.contacts["c2"]["title"] = "Head of Sales"
    rows, _ = _sync(fake, state)

    assert _ids(rows) == {"contact:c2"}
    assert "Job title: Head of Sales" in rows[0]["content"]


def test_an_enrollment_re_emits_the_contact_although_updated_at_did_not_move():
    fake, state = _workspace(), {}
    _sync(fake, state)
    updated_at = fake.contacts["c1"]["updated_at"]

    fake.enroll("c1", "s1", status="paused")
    rows, _ = _sync(fake, state)

    assert fake.contacts["c1"]["updated_at"] == updated_at
    assert _ids(rows) == {"contact:c1"}
    lines = rows[0]["content"].splitlines()
    assert "Sequences: Q4 Outreach (paused)" in lines
    assert lines[-2:] == ["Sequence activity:", "2026-10-01 enrolled: Q4 Outreach"]


def test_the_activity_feed_is_read_only_for_enrolled_contacts_that_changed():
    fake, state = _workspace(), {}
    fake.enroll("c1", "s1")
    first, _ = _sync(fake, state)
    assert fake.count("/emailer_campaigns/activity_feed") == 1

    _sync(fake, state)
    assert fake.count("/emailer_campaigns/activity_feed") == 1

    fake.contacts["c1"]["last_activity_date"] = "2026-10-02T08:00:00.000Z"
    rows, _ = _sync(fake, state)
    assert fake.count("/emailer_campaigns/activity_feed") == 2
    # nothing visible changed, so cognee keeps the document's data_id
    assert rows == [row for row in first if row["id"] == "contact:c1"]


def test_deleted_contacts_and_accounts_are_tombstoned_after_a_check():
    fake, state = _workspace(), {}
    _sync(fake, state)

    del fake.contacts["c2"]
    del fake.accounts["a2"]
    rows, stats = _sync(fake, state)

    assert rows == [{"id": "account:a2", "_deleted": True}, {"id": "contact:c2", "_deleted": True}]
    assert stats["deleted"] == 2
    assert fake.count("/contacts/c2") == 1
    assert fake.count("/accounts/a2") == 1
    assert "contact:c2" not in state["fingerprints"]


def test_a_removed_sequence_is_tombstoned():
    fake, state = _workspace(), {}
    _sync(fake, state)

    del fake.sequences["s1"]
    rows, _ = _sync(fake, state)

    assert _tombstones(rows) == {"sequence:s1"}


def test_a_record_missing_from_search_but_still_in_scope_is_kept():
    fake, state = _workspace(), {}
    _sync(fake, state)

    fake.hidden.add("c2")
    rows, _ = _sync(fake, state)

    assert rows == []
    assert "contact:c2" in state["fingerprints"]


def test_a_contact_that_left_the_selected_stage_is_forgotten():
    fake, state = _workspace(), {}
    _sync(fake, state, include=("contacts",), filters={"contact_stage_ids": ["cs1"]})

    fake.contacts["c2"]["contact_stage_id"] = "cs2"
    rows, _ = _sync(fake, state, include=("contacts",), filters={"contact_stage_ids": ["cs1"]})

    assert _tombstones(rows) == {"contact:c2"}


def test_a_contact_that_left_the_selected_list_is_forgotten():
    fake, state = _workspace(), {}
    filters = {"contact_label_ids": ["l1"]}
    fake.contacts["c1"]["label_ids"] = ["l1"]
    fake.contacts["c2"]["label_ids"] = ["l1", "l2"]
    _sync(fake, state, include=("contacts",), filters=filters)

    fake.contacts["c2"]["label_ids"] = ["l2"]
    fake.hidden.add("c1")  # missing from search but still in the list
    rows, _ = _sync(fake, state, include=("contacts",), filters=filters)

    assert _tombstones(rows) == {"contact:c2"}


def test_deselecting_a_kind_forgets_its_records():
    fake, state = _workspace(), {}
    _sync(fake, state)

    rows, _ = _sync(fake, state, include=("contacts",))

    assert _tombstones(rows) == {"sequence:s1", "account:a1", "account:a2"}
    assert fake.count("/accounts/a1") == 0  # deselected records need no check


def test_a_rate_limited_run_stops_cleanly_and_resumes_without_false_deletes():
    fake, state = _workspace(), {}
    _sync(fake, state)
    del fake.contacts["c2"]

    fake.fail_after = len(fake.calls) + 6  # lookups and accounts pass, contacts search fails
    fake.fail_with = ApolloRateLimitedError("Apollo rate limit exceeded (HTTP 429)")
    rows, stats = _sync(fake, state)

    assert rows == []
    assert stats["failed_rate_limit"] == 1
    assert "contact:c2" in state["fingerprints"]

    fake.fail_after = None
    rows, stats = _sync(fake, state)

    assert _tombstones(rows) == {"contact:c2"}
    assert stats["failed"] == 0


def test_a_cut_during_listing_resumes_at_the_same_page():
    fake, state = FakeApollo(), {}
    for n in range(PAGE_SIZE * 2 + 50):
        fake.add_contact(f"c{n:03d}", f"Person {n}")
    rows, stats = _sync(fake, state, include=("contacts",), max_requests=7)

    assert stats["failed_budget"] == 1
    assert len(rows) == PAGE_SIZE * 2  # lookups take five requests, then two pages
    assert state["walks"]["contacts"]["page"] == 3

    rows, stats = _sync(fake, state, include=("contacts",))

    assert len(rows) == 50
    assert stats["failed"] == 0
    assert _tombstones(rows) == set()


@pytest.mark.parametrize("window", ["minute", "hourly", "daily"])
def test_low_remaining_quota_stops_the_run_before_the_next_request(window):
    fake, state = _workspace(), {}
    fake.rate_limit = {"minute": 40, "hourly": 400, "daily": 500, window: 3}

    rows, stats = _sync(fake, state)

    assert rows == []
    assert stats["failed_rate_limit"] == 1
    assert fake.calls == []


def test_an_auth_error_before_any_row_raises():
    fake = _workspace()
    fake.fail_after = 0
    fake.fail_with = ApolloAuthError("Apollo rejected the API key (HTTP 401)")

    with pytest.raises(ApolloAuthError):
        _sync(fake, {})


def test_a_capped_listing_never_tombstones():
    fake, state = _workspace(), {}
    _sync(fake, state)

    del fake.contacts["c2"]
    fake.total_override = 60_000
    rows, stats = _sync(fake, state)

    assert _tombstones(rows) == set()
    assert stats["failed_cap"] == 1


@pytest.mark.parametrize(
    "error",
    [ApolloAPIError("Apollo request failed: HTTP 500"), ApolloAccessError("HTTP 403")],
)
def test_a_failing_check_keeps_that_record_and_the_sync_moves_on(error):
    fake, state = _workspace(), {}
    _sync(fake, state)

    fake.hidden.add("c2")
    fake.fail_paths["/contacts/c2"] = error
    del fake.contacts["c1"]
    rows, stats = _sync(fake, state)

    assert _tombstones(rows) == {"contact:c1"}
    assert stats["failed"] == 1
    assert "contact:c2" in state["fingerprints"]

    fake.add_contact("c3", "Cara Hopper")
    rows, stats = _sync(fake, state)

    assert _ids(rows) == {"contact:c3"}
    assert stats["failed"] == 1
    assert "contact:c2" in state["fingerprints"]


def test_a_check_answered_without_a_record_keeps_it():
    fake, state = _workspace(), {}
    filters = {"contact_stage_ids": ["cs1"]}
    _sync(fake, state, include=("contacts",), filters=filters)

    fake.hidden.update({"c1", "c2"})
    fake.answers["/contacts/c1"] = {}
    fake.answers["/contacts/c2"] = {"contact": None}
    rows, stats = _sync(fake, state, include=("contacts",), filters=filters)

    assert rows == []
    assert stats["failed"] == 2
    assert {"contact:c1", "contact:c2"} <= set(state["fingerprints"])


@pytest.mark.parametrize("path", ["/emailer_campaigns/search", "/contacts/search"])
def test_a_listing_without_pagination_fails_instead_of_forgetting(path):
    fake, state = _workspace(), {}
    for n in range(PAGE_SIZE + 50):
        fake.add_sequence(f"s{n:03d}", f"Sequence {n}")
    _sync(fake, state)

    fake.no_pagination.add(path)
    with pytest.raises(ApolloAPIError, match="pagination"):
        _sync(fake, state)

    assert state.get("pending_deletes") == []
    assert len(state["fingerprints"]) == PAGE_SIZE + 50 + 5


def test_changing_the_filters_restarts_the_listing():
    fake, state = FakeApollo(), {}
    for n in range(PAGE_SIZE * 3):
        fake.add_contact(f"c{n:03d}", f"Person {n}", contact_stage_id="cs1" if n % 2 else "cs2")
    _sync(
        fake, state, include=("contacts",), filters={"contact_stage_ids": ["cs1"]}, max_requests=6
    )
    assert state["walks"]["contacts"]["page"] == 2

    rows, stats = _sync(fake, state, include=("contacts",), filters={"contact_stage_ids": ["cs2"]})

    assert stats["failed"] == 0
    emitted = {row["id"] for row in rows if not row["_deleted"]}
    assert emitted == {f"contact:c{n:03d}" for n in range(0, PAGE_SIZE * 3, 2)}


def test_an_activity_feed_failure_skips_only_that_contact_until_the_next_run():
    fake, state = _workspace(), {}
    fake.enroll("c1", "s1")
    fake.fail_paths["/emailer_campaigns/activity_feed"] = ApolloAPIError("HTTP 500")

    rows, stats = _sync(fake, state)

    assert "contact:c1" not in _ids(rows)
    assert "contact:c2" in _ids(rows)
    assert stats["failed"] == 1

    del fake.fail_paths["/emailer_campaigns/activity_feed"]
    rows, stats = _sync(fake, state)

    assert _ids(rows) == {"contact:c1"}
    assert "Sequence activity:" in rows[0]["content"]
    assert stats["failed"] == 0


def test_a_full_activity_feed_is_marked_as_cut():
    fake, state = _workspace(), {}
    fake.enroll("c1", "s1")
    fake.events["c1"] *= 60

    rows, _ = _sync(fake, state)

    content = next(row["content"] for row in rows if row["id"] == "contact:c1")
    assert "Sequence activity (cut at 50 events):" in content.splitlines()


def test_check_active_runs_before_every_request_and_can_stop_the_sync():
    fake, checks = _workspace(), []

    def check_active():
        checks.append(len(fake.calls))
        if len(checks) > 3:
            raise PermissionError("access revoked")

    with pytest.raises(PermissionError):
        _sync(fake, {}, check_active=check_active)

    assert checks == [0, 1, 2, 3]
    assert len(fake.calls) == 3


def test_state_of_another_apollo_team_is_refused():
    state = {}
    _sync(_workspace(), state)

    other = FakeApollo(team_id="team2")
    other.add_contact("c9", "Dev Hollerith")
    with pytest.raises(ApolloSourceError, match="resource_name"):
        _sync(other, state)

    assert set(state["fingerprints"]) >= {"contact:c1", "contact:c2"}


def _shuffled(contact: dict, events: list[dict], seed: int) -> tuple[dict, list[dict]]:
    rng = random.Random(seed)
    contact = dict(contact)
    for key in ("label_ids", "contact_campaign_statuses"):
        contact[key] = rng.sample(contact[key], len(contact[key]))
    fields = list(contact["typed_custom_fields"].items())
    contact["typed_custom_fields"] = dict(rng.sample(fields, len(fields)))
    return contact, rng.sample(events, len(events))


def test_a_contact_renders_the_same_whatever_order_apollo_sends():
    fake = FakeApollo()
    fake.add_sequence("s1", "Q4 Outreach")
    fake.add_sequence("s2", "Renewals")
    contact = fake.add_contact(
        "c1",
        "Ada Lovelace",
        label_ids=["l1", "l2"],
        typed_custom_fields={"f1": "Data quality", "f2": "Gold"},
        contact_campaign_statuses=[
            {"id": "x1", "emailer_campaign_id": "s1", "status": "active"},
            {"id": "x2", "emailer_campaign_id": "s2", "status": "paused"},
        ],
    )
    events = [
        {"type": t, "occurred_at": f"2026-10-0{d}T09:00:00Z", "sequence_id": s}
        for d, t, s in [(1, "enrolled", "s1"), (1, "enrolled", "s2"), (2, "replied", "s1")]
    ]
    lookups = Lookups(
        labels={"l1": "Champions", "l2": "Key Accounts"},
        fields={"f1": "Messaging Angle", "f2": "Tier"},
        sequences={"s1": "Q4 Outreach", "s2": "Renewals"},
    )

    expected = render_contact(contact, lookups, events)
    for seed in range(20):
        shuffled_contact, shuffled_events = _shuffled(contact, events, seed)
        assert render_contact(shuffled_contact, lookups, shuffled_events) == expected


def test_a_reordered_contact_is_not_re_emitted_or_re_read():
    fake, state = FakeApollo(), {}
    fake.add_sequence("s1", "Q4 Outreach")
    fake.add_sequence("s2", "Renewals")
    fake.add_contact(
        "c1",
        "Ada Lovelace",
        label_ids=["l1", "l2"],
        typed_custom_fields={"f1": "Data quality", "f2": "Gold"},
    )
    fake.enroll("c1", "s1")
    fake.contacts["c1"]["contact_campaign_statuses"].append(
        {"id": "x2", "emailer_campaign_id": "s2", "status": "paused"}
    )
    _sync(fake, state, include=("contacts",))
    feed_reads = fake.count("/emailer_campaigns/activity_feed")

    contact = fake.contacts["c1"]
    for key in ("label_ids", "contact_campaign_statuses"):
        contact[key] = contact[key][::-1]
    contact["typed_custom_fields"] = dict(reversed(contact["typed_custom_fields"].items()))
    rows, stats = _sync(fake, state, include=("contacts",))

    assert rows == []
    assert stats["skipped"] == 1
    assert fake.count("/emailer_campaigns/activity_feed") == feed_reads


def test_a_cut_while_forgetting_finishes_the_pending_deletes_next_run():
    fake, state = _workspace(), {}
    _sync(fake, state)
    del fake.contacts["c1"]
    del fake.contacts["c2"]

    fake.fail_paths["/contacts/c2"] = ApolloRateLimitedError("Apollo rate limit exceeded")
    rows, stats = _sync(fake, state)

    assert _tombstones(rows) == {"contact:c1"}
    assert stats["failed_rate_limit"] == 1
    assert state["pending_deletes"] == ["contact:c2"]

    del fake.fail_paths["/contacts/c2"]
    rows, _ = _sync(fake, state)

    assert _tombstones(rows) == {"contact:c2"}
