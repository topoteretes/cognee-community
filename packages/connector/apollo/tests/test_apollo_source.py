"""The sync walk, driven with a plain dict as dlt resource state."""

import pytest
from fake_apollo import FakeApollo

from cognee_community_connector_apollo.apollo import (
    PAGE_SIZE,
    ApolloAPIError,
    ApolloAuthError,
    ApolloRateLimitedError,
    _iter_rows,
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


def test_low_remaining_quota_stops_the_run_before_the_next_request():
    fake, state = _workspace(), {}
    fake.rate_limit = {"hourly": 3, "daily": 500}

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


def test_an_unexpected_error_during_the_check_aborts_instead_of_forgetting():
    fake, state = _workspace(), {}
    _sync(fake, state)

    fake.hidden.add("c2")
    fake.fail_paths["/contacts/c2"] = ApolloAPIError("Apollo request failed: HTTP 422")
    with pytest.raises(ApolloAPIError):
        _sync(fake, state)

    assert "contact:c2" in state["fingerprints"]


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
