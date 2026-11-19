"""The sync walk, driven with a plain dict as dlt resource state."""

import random

import pytest
from fake_copper import FakeCopper

from cognee_community_connector_copper import copper as copper_module
from cognee_community_connector_copper.copper import (
    CopperAccessError,
    CopperAPIError,
    CopperAuthError,
    CopperRateLimitedError,
    CopperSourceError,
    _iter_rows,
)

ROW_COLUMNS = {"id", "title", "content", "url", "_deleted"}
APP = "https://app.copper.com/companies/1/app#"


def _sync(fake: FakeCopper, state: dict, **kwargs) -> tuple[list[dict], dict]:
    stats: dict[str, int] = {}
    rows = list(_iter_rows(fake, state, stats, **kwargs))
    return rows, stats


def _ids(rows: list[dict]) -> set[str]:
    return {row["id"] for row in rows}


def _tombstones(rows: list[dict]) -> set[str]:
    return {row["id"] for row in rows if row.get("_deleted")}


def _by_id(rows: list[dict]) -> dict[str, dict]:
    return {row["id"]: row for row in rows}


def _account() -> FakeCopper:
    fake = FakeCopper()
    fake.add_company(1, "Acme Corp", details="Long-time customer in logistics.")
    fake.add_person(2, "Ada Lovelace", company_id=1, company_name="Acme Corp")
    fake.add_opportunity(
        3, "Acme renewal", company_id=1, company_name="Acme Corp", primary_contact_id=2
    )
    fake.add_activity(4, "Ada wants SSO before signing.", ("opportunity", 3))
    return fake


ALL = {"company:1", "person:2", "opportunity:3", "activity:4"}


@pytest.fixture
def small_pages(monkeypatch):
    monkeypatch.setattr(copper_module, "PAGE_SIZE", 2)


def test_first_sync_emits_every_record_with_the_frozen_row_shape():
    rows, stats = _sync(_account(), {})

    assert _ids(rows) == ALL
    assert all(set(row) == ROW_COLUMNS for row in rows)
    assert all(row["_deleted"] is False and row["title"] and row["content"] for row in rows)
    urls = {row["id"]: row["url"] for row in rows}
    assert urls["company:1"] == f"{APP}/organization/1"
    assert urls["person:2"] == f"{APP}/contact/2"
    assert urls["opportunity:3"] == f"{APP}/deal/3"
    # an activity links to the record it was logged on
    assert urls["activity:4"] == f"{APP}/deal/3"
    assert stats == {"scanned": 4, "skipped": 0, "deleted": 0, "failed": 0}


def test_every_request_sends_what_copper_documents():
    fake = _account()
    _sync(fake, {})

    searches = {path: body for method, path, body in fake.calls if path.endswith("/search")}
    assert searches["/people/search"]["page_size"] == 200
    assert searches["/people/search"]["page_number"] == 1
    # only enabled user activity types are searched
    assert searches["/activities/search"]["activity_types"] == [
        {"id": 0, "category": "user"},
        {"id": 61, "category": "user"},
    ]


def test_activities_carry_their_text_type_and_parent():
    rows = _by_id(_sync(_account(), {})[0])

    activity = rows["activity:4"]
    assert activity["title"] == "Note on Acme renewal"
    assert "Ada wants SSO before signing." in activity["content"]
    assert "Opportunity: Acme renewal" in activity["content"]
    assert "By: Sam Seller" in activity["content"]
    assert "Date: 2025-10-20" in activity["content"]


def test_ids_are_rendered_as_names():
    fake = _account()
    fake.opportunities[3]["custom_fields"] = [
        {"custom_field_definition_id": 51, "value": 7},
        {"custom_field_definition_id": 52, "value": 1_798_761_600},
        {"custom_field_definition_id": 999, "value": "unknown"},
    ]
    fake.people[2]["custom_fields"] = [{"custom_field_definition_id": 53, "value": "Sheets"}]
    rows = _by_id(_sync(fake, {})[0])

    opportunity = rows["opportunity:3"]["content"]
    for line in (
        "Status: Open",
        "Pipeline: Sales",
        "Stage: Qualified",
        "Value: 5000 USD",
        "Company: Acme Corp",
        "Primary contact: Ada Lovelace",
        "Owner: Sam Seller",
        "Source: Email",
        "Contract length: 12 months",
        "Renewal date: 2027-01-01",
    ):
        assert line in opportunity
    assert "unknown" not in opportunity  # a field missing from the definitions is left out
    person = rows["person:2"]["content"]
    assert "Favourite tool: Sheets" in person
    assert "Type: Potential Customer" in person
    assert "Long-time customer in logistics." in rows["company:1"]["content"]


def test_contact_channels_and_counters_are_never_rendered():
    rows, _ = _sync(_account(), {})
    text = " ".join(f"{row['title']} {row['content']}" for row in rows)

    assert "@example.com" not in text
    assert "555 0100" not in text
    assert "interaction" not in text.lower()


def test_system_activities_and_activities_on_leads_are_left_out():
    fake = _account()
    fake.add_activity(5, "Stage changed", ("opportunity", 3), type_id=1)
    fake.add_activity(6, "Lead note", ("lead", 9))
    fake.add_activity(7, "Old type", ("person", 2), type_id=62)

    rows, _ = _sync(fake, {})

    assert _ids(rows) == ALL


def test_a_quiet_second_run_emits_nothing():
    fake, state = _account(), {}
    _sync(fake, state)

    rows, stats = _sync(fake, state)

    assert rows == []
    assert stats["skipped"] == 4 and stats["failed"] == 0


def test_only_the_changed_record_is_emitted_again():
    fake, state = _account(), {}
    _sync(fake, state)

    fake.activities[4]["details"] = "Ada signed."
    rows, _ = _sync(fake, state)

    assert _ids(rows) == {"activity:4"}
    assert "Ada signed." in rows[0]["content"]


def test_renaming_an_opportunity_refreshes_its_activities():
    fake, state = _account(), {}
    _sync(fake, state)

    fake.opportunities[3]["name"] = "Acme expansion"
    rows = _by_id(_sync(fake, state)[0])

    assert set(rows) == {"opportunity:3", "activity:4"}
    assert rows["activity:4"]["title"] == "Note on Acme expansion"


def test_rendering_does_not_depend_on_tag_or_field_order():
    first, second = _account(), _account()
    first.opportunities[3].update(
        tags=["vip", "q4"],
        custom_fields=[
            {"custom_field_definition_id": 51, "value": 7},
            {"custom_field_definition_id": 52, "value": 1},
        ],
    )
    second.opportunities[3].update(
        tags=["q4", "vip"],
        custom_fields=[
            {"custom_field_definition_id": 52, "value": 1},
            {"custom_field_definition_id": 51, "value": 7},
        ],
    )

    assert _by_id(_sync(first, {})[0]) == _by_id(_sync(second, {})[0])


def test_a_deleted_record_is_rechecked_then_forgotten():
    fake, state = _account(), {}
    _sync(fake, state)

    del fake.activities[4]
    del fake.people[2]
    rows, stats = _sync(fake, state)

    assert _tombstones(rows) == {"activity:4", "person:2"}
    assert stats["deleted"] == 2
    assert fake.count("/activities/4") == 1 and fake.count("/people/2") == 1


def test_a_record_missing_from_a_search_but_still_there_is_kept():
    fake, state = _account(), {}
    _sync(fake, state)

    fake.hidden.add("opportunities:3")
    rows, _ = _sync(fake, state)

    assert _tombstones(rows) == set()
    assert fake.count("/opportunities/3") == 1


def test_disabling_an_activity_type_forgets_its_activities():
    fake, state = _account(), {}
    fake.add_activity(5, "Called Ada", ("person", 2), type_id=61)
    _sync(fake, state)

    fake.activity_types["user"][1]["is_disabled"] = True
    rows, _ = _sync(fake, state)

    assert _tombstones(rows) == {"activity:5"}


def test_deselecting_a_kind_forgets_its_records_and_their_activities():
    fake, state = _account(), {}
    fake.add_activity(5, "Called Ada", ("person", 2), type_id=61)
    _sync(fake, state)

    rows, _ = _sync(fake, state, include=("opportunities", "activities"))

    assert _tombstones(rows) == {"company:1", "person:2", "activity:5"}
    assert fake.count("/people/2") == 0
    assert fake.count("/activities/5") == 1  # still there, but its parent kind is out


def test_a_failing_recheck_keeps_the_record_and_does_not_block_the_run():
    fake, state = _account(), {}
    _sync(fake, state)

    del fake.people[2]
    fake.fail_paths["/people/2"] = CopperAPIError("Copper request failed: HTTP 500")
    fake.add_opportunity(6, "Globex pilot")
    rows, stats = _sync(fake, state)

    assert _ids(rows) == {"opportunity:6"}
    assert stats["failed"] == 1

    del fake.fail_paths["/people/2"]
    rows, _ = _sync(fake, state)
    assert _tombstones(rows) == {"person:2"}


@pytest.mark.parametrize("answer", [{}, [], None, "<html>login</html>"])
def test_a_recheck_answered_with_no_record_keeps_it(answer):
    fake, state = _account(), {}
    _sync(fake, state)

    fake.hidden.add("people:2")
    fake.answers["/people/2"] = answer
    rows, stats = _sync(fake, state)

    assert _tombstones(rows) == set()
    assert stats["failed"] == 1


def test_an_empty_search_does_not_forget_records_still_in_copper():
    fake, state = _account(), {}
    _sync(fake, state)

    fake.hidden.update({"companies:1", "people:2", "opportunities:3", "activities:4"})
    rows, _ = _sync(fake, state)

    assert _tombstones(rows) == set()


def test_a_search_answered_with_an_object_raises_instead_of_looking_empty():
    fake, state = _account(), {}
    _sync(fake, state)

    fake.answers["/people/search"] = {"error": "Unauthorized"}
    with pytest.raises(CopperAPIError, match="no list"):
        _sync(fake, state)


def test_paging_reads_every_page(small_pages):
    fake = FakeCopper()
    for i in range(1, 6):
        fake.add_opportunity(i, f"Deal {i}")

    rows, _ = _sync(fake, {}, include=("opportunities",))

    assert len(rows) == 5
    pages = [b["page_number"] for _, p, b in fake.calls if p == "/opportunities/search"]
    assert pages == [1, 2, 3]


def test_a_capped_search_never_decides_deletions(monkeypatch, small_pages):
    monkeypatch.setattr(copper_module, "SEARCH_CAP", 4)
    fake, state = FakeCopper(), {}
    for i in range(1, 6):
        fake.add_opportunity(i, f"Deal {i}")

    rows, stats = _sync(fake, state, include=("opportunities",))
    assert len(rows) == 4
    assert stats["failed_cap"] == 1

    del fake.opportunities[1]
    rows, _ = _sync(fake, state, include=("opportunities",))
    assert _tombstones(rows) == set()


def test_a_rate_limited_run_resumes_where_it_stopped(small_pages):
    fake, state = FakeCopper(), {}
    for i in range(1, 6):
        fake.add_opportunity(i, f"Deal {i}")
    fake.fail_from = ("/opportunities/search", 3)  # two pages, then 429
    fake.fail_with = CopperRateLimitedError("Copper rate limit exceeded (HTTP 429)")

    rows, stats = _sync(fake, state, include=("opportunities",))
    assert _ids(rows) == {"opportunity:1", "opportunity:2", "opportunity:3", "opportunity:4"}
    assert stats["failed_rate_limit"] == 1

    fake.fail_after = fake.fail_from = None
    fake.calls.clear()
    rows, stats = _sync(fake, state, include=("opportunities",))
    assert _ids(rows) == {"opportunity:5"}
    assert stats["failed"] == 0
    # the cut run's pages are not read again
    pages = [b["page_number"] for _, p, b in fake.calls if p == "/opportunities/search"]
    assert pages == [3]


def test_the_request_budget_cuts_a_run_and_nothing_is_forgotten_early(small_pages):
    fake, state = FakeCopper(), {}
    for i in range(1, 6):
        fake.add_opportunity(i, f"Deal {i}")
    _sync(fake, state, include=("opportunities",))

    del fake.opportunities[1]
    # 6 lookup requests, then the first page; the second page is cut
    rows, stats = _sync(fake, state, include=("opportunities",), max_requests=7)
    assert stats["failed_budget"] == 1 and stats["scanned"] == 2
    assert _tombstones(rows) == set()

    rows, _ = _sync(fake, state, include=("opportunities",))
    assert _tombstones(rows) == {"opportunity:1"}


def test_a_bad_key_raises_but_a_key_revoked_mid_run_keeps_progress(small_pages):
    fake = _account()
    fake.fail_paths["/account"] = CopperAuthError("Copper rejected the API key")
    with pytest.raises(CopperAuthError):
        _sync(fake, {})

    fake = FakeCopper()
    for i in range(1, 4):
        fake.add_opportunity(i, f"Deal {i}")
    fake.fail_from = ("/opportunities/search", 2)
    fake.fail_with = CopperAuthError("Copper rejected the API key")
    rows, stats = _sync(fake, {}, include=("opportunities",))
    assert _ids(rows) == {"opportunity:1", "opportunity:2"}
    assert stats["failed_auth"] == 1


def test_a_search_the_user_cannot_read_raises():
    fake = _account()
    fake.fail_paths["/activities/search"] = CopperAccessError("HTTP 403")

    with pytest.raises(CopperAccessError):
        _sync(fake, {})


def test_state_from_another_account_is_refused():
    state = {}
    _sync(_account(), state)

    with pytest.raises(CopperSourceError, match="another Copper account"):
        _sync(FakeCopper(account_id=2), state)


def test_check_active_is_asked_before_every_request():
    fake, asked = _account(), []
    _sync(fake, {}, check_active=lambda: asked.append(len(fake.calls)))

    assert len(asked) >= len(fake.calls)


def test_a_shuffled_search_renders_the_same_rows():
    fake = _account()
    for i in range(10, 30):
        fake.add_activity(i, f"Note {i}", ("opportunity", 3))
    first = _by_id(_sync(fake, {})[0])

    items = list(fake.activities.items())
    random.Random(4).shuffle(items)
    fake.activities = dict(items)

    assert _by_id(_sync(fake, {})[0]) == first


def test_a_record_that_fails_to_render_does_not_block_the_account():
    fake, state = _account(), {}
    # an owner id of an unexpected shape, which a plain dict lookup cannot hash
    fake.opportunities[3]["assignee_id"] = {"id": 100}
    # a field id that is a list, so checking it against the definitions raises
    fake.companies[1]["custom_fields"] = [{"custom_field_definition_id": [51], "value": 7}]
    rows, stats = _sync(fake, state)

    assert "opportunity:3" in _ids(rows)  # the odd owner is left out
    assert "company:1" not in _ids(rows)
    assert stats["failed"] == 1
    # the cycle still closed: a deleted record is forgotten in the next run
    del fake.activities[4]
    rows, _ = _sync(fake, state)
    assert _tombstones(rows) == {"activity:4"}
