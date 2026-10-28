"""The sync walk, driven with a plain dict as dlt resource state."""

import random

import pytest
from fake_pipedrive import DEAL_FIELD_KEY, PERSON_FIELD_KEY, PHONE_FIELD_KEY, FakePipedrive

from cognee_community_connector_pipedrive.pipedrive import (
    PipedriveAccessError,
    PipedriveAPIError,
    PipedriveAuthError,
    PipedriveRateLimitedError,
    PipedriveSourceError,
    _iter_rows,
    html_to_text,
)

ROW_COLUMNS = {"id", "title", "content", "url", "_deleted"}


def _sync(fake: FakePipedrive, state: dict, **kwargs) -> tuple[list[dict], dict]:
    stats: dict[str, int] = {}
    rows = list(_iter_rows(fake, state, stats, **kwargs))
    return rows, stats


def _ids(rows: list[dict]) -> set[str]:
    return {row["id"] for row in rows}


def _tombstones(rows: list[dict]) -> set[str]:
    return {row["id"] for row in rows if row.get("_deleted")}


def _by_id(rows: list[dict]) -> dict[str, dict]:
    return {row["id"]: row for row in rows}


def _company() -> FakePipedrive:
    fake = FakePipedrive()
    fake.add_organization(1, "Acme Corp")
    fake.add_person(2, "Ada Lovelace", org_id=1)
    fake.add_deal(3, "Acme renewal", org_id=1, person_id=2)
    fake.add_activity(4, "Kickoff call", deal_id=3, note="<p>Talked pricing</p>")
    fake.add_note(5, "<p>Ada wants SSO before signing.</p>", deal_id=3)
    return fake


ALL = {"organization:1", "person:2", "deal:3", "activity:4", "note:5"}


def test_first_sync_emits_every_record_with_the_frozen_row_shape():
    rows, stats = _sync(_company(), {})

    assert _ids(rows) == ALL
    assert all(set(row) == ROW_COLUMNS for row in rows)
    assert all(row["_deleted"] is False and row["title"] and row["content"] for row in rows)
    urls = {row["id"]: row["url"] for row in rows}
    assert urls["deal:3"] == "https://acme.pipedrive.com/deal/3"
    assert urls["person:2"] == "https://acme.pipedrive.com/person/2"
    assert urls["organization:1"] == "https://acme.pipedrive.com/organization/1"
    # notes and activities link to the deal they belong to
    assert urls["note:5"] == urls["activity:4"] == "https://acme.pipedrive.com/deal/3"
    assert stats == {"scanned": 5, "skipped": 0, "deleted": 0, "failed": 0}


def test_notes_and_activities_carry_their_text_and_parent_names():
    rows = _by_id(_sync(_company(), {})[0])

    note = rows["note:5"]
    assert note["title"] == "Note on deal Acme renewal"
    assert "Ada wants SSO before signing." in note["content"]
    assert "Organization: Acme Corp" not in note["content"]  # the note only names its deal
    assert "Author: Sam Seller" in note["content"]
    assert "<p>" not in note["content"]

    activity = rows["activity:4"]
    assert "Deal: Acme renewal" in activity["content"]
    assert "Talked pricing" in activity["content"]
    assert "Type: call" in activity["content"]


def test_ids_are_rendered_as_names():
    fake = _company()
    fake.deals[3].update(label_ids=[1], custom_fields={DEAL_FIELD_KEY: 7, "c" * 40: "unknown"})
    fake.persons[2]["custom_fields"] = {PERSON_FIELD_KEY: "Spreadsheets"}
    rows = _by_id(_sync(fake, {})[0])

    deal = rows["deal:3"]["content"]
    for line in (
        "Pipeline: Sales",
        "Stage: Qualified",
        "Value: 5000 USD",
        "Organization: Acme Corp",
        "Person: Ada Lovelace",
        "Owner: Sam Seller",
        "Labels: Hot",
        "Contract length: 12 months",
    ):
        assert line in deal
    assert "unknown" not in deal  # a field missing from the definitions is left out
    assert "Favourite tool: Spreadsheets" in rows["person:2"]["content"]
    assert "Organization: Acme Corp" in rows["person:2"]["content"]


def test_contact_channels_are_never_rendered():
    fake = _company()
    fake.persons[2]["custom_fields"] = {PHONE_FIELD_KEY: "+1 555 0199"}
    rows, _ = _sync(fake, {})
    text = " ".join(f"{row['title']} {row['content']}" for row in rows)

    assert "@example.com" not in text
    assert "555 0100" not in text
    assert "555 0199" not in text and "Assistant phone" not in text


def test_a_quiet_second_run_emits_nothing():
    fake, state = _company(), {}
    _sync(fake, state)

    rows, stats = _sync(fake, state)

    assert rows == []
    assert stats["skipped"] == 5 and stats["failed"] == 0


def test_only_the_changed_record_is_emitted_again():
    fake, state = _company(), {}
    _sync(fake, state)

    fake.notes[5]["content"] = "<p>Ada signed.</p>"
    rows, _ = _sync(fake, state)

    assert _ids(rows) == {"note:5"}
    assert "Ada signed." in rows[0]["content"]


def test_renaming_a_deal_refreshes_the_notes_and_activities_that_name_it():
    fake, state = _company(), {}
    _sync(fake, state)

    fake.deals[3]["title"] = "Acme expansion"
    rows = _by_id(_sync(fake, state)[0])

    assert set(rows) == {"deal:3", "activity:4", "note:5"}
    assert rows["note:5"]["title"] == "Note on deal Acme expansion"


def test_rendering_does_not_depend_on_label_or_field_order():
    first, second = _company(), _company()
    first.deals[3]["label_ids"] = [1, 2]
    second.deals[3]["label_ids"] = [2, 1]
    first.deals[3]["custom_fields"] = {DEAL_FIELD_KEY: [8, 7]}
    second.deals[3]["custom_fields"] = {DEAL_FIELD_KEY: [7, 8]}

    assert _by_id(_sync(first, {})[0]) == _by_id(_sync(second, {})[0])


def test_a_deleted_record_is_rechecked_then_forgotten():
    fake, state = _company(), {}
    _sync(fake, state)

    del fake.notes[5]
    fake.persons[2]["is_deleted"] = True
    rows, stats = _sync(fake, state)

    assert _tombstones(rows) == {"note:5", "person:2"}
    assert stats["deleted"] == 2
    assert fake.count("/v1/notes/5") == 1 and fake.count("/api/v2/persons/2") == 1


def test_a_record_missing_from_a_listing_but_still_there_is_kept():
    fake, state = _company(), {}
    _sync(fake, state)

    fake.hidden.add("deals:3")
    rows, _ = _sync(fake, state)

    assert _tombstones(rows) == set()
    assert fake.count("/api/v2/deals/3") == 1


def test_an_archived_deal_is_kept_unless_archived_deals_are_left_out():
    fake, state = _company(), {}
    _sync(fake, state)

    fake.deals[3]["is_archived"] = True
    rows, _ = _sync(fake, state)
    assert _ids(rows) == {"deal:3"}
    assert "Status: open (archived)" in rows[0]["content"]

    rows, _ = _sync(fake, state, include_archived=False)
    assert _tombstones(rows) == {"deal:3"}


def test_an_archived_deal_in_the_regular_listing_is_still_forgotten_when_left_out():
    fake, state = _company(), {}
    fake.archived_in_listing = True
    _sync(fake, state)

    fake.deals[3]["is_archived"] = True
    rows, _ = _sync(fake, state, include_archived=False)

    assert _tombstones(rows) == {"deal:3"}


def test_a_record_that_fails_to_render_does_not_block_the_company():
    fake, state = _company(), {}
    # an owner id of an unexpected shape, which a plain dict lookup cannot hash
    fake.deals[3]["owner_id"] = {"id": 100}
    fake.organizations[1]["label_ids"] = 5  # not a list, so rendering it raises
    rows, stats = _sync(fake, state)

    assert "deal:3" in _ids(rows)  # the odd owner is left out, the deal still renders
    assert "organization:1" not in _ids(rows)
    assert stats["failed"] == 1
    # the cycle still closed: a deleted record is forgotten in the same run
    del fake.notes[5]
    rows, _ = _sync(fake, state)
    assert _tombstones(rows) == {"note:5"}


def test_a_deleted_deal_is_forgotten():
    fake, state = _company(), {}
    _sync(fake, state)

    fake.deals[3].update(is_deleted=True, status="deleted")
    rows, _ = _sync(fake, state)

    assert _tombstones(rows) == {"deal:3"}


def test_deselecting_a_kind_forgets_its_records_without_requests():
    fake, state = _company(), {}
    _sync(fake, state)

    rows, _ = _sync(fake, state, include=("deals", "notes"))

    assert _tombstones(rows) == {"organization:1", "person:2", "activity:4"}
    assert fake.count("/api/v2/persons/2") == 0


def test_a_failing_recheck_keeps_the_record_and_does_not_block_the_run():
    fake, state = _company(), {}
    _sync(fake, state)

    del fake.notes[5]
    fake.fail_paths["/v1/notes/5"] = PipedriveAPIError("Pipedrive request failed: HTTP 500")
    fake.add_deal(6, "Globex pilot")
    rows, stats = _sync(fake, state)

    assert _ids(rows) == {"deal:6"}
    assert stats["failed"] == 1

    del fake.fail_paths["/v1/notes/5"]
    rows, _ = _sync(fake, state)
    assert _tombstones(rows) == {"note:5"}


def test_a_recheck_answered_with_no_record_keeps_it():
    fake, state = _company(), {}
    _sync(fake, state)

    fake.hidden.add("notes:5")
    fake.answers["/v1/notes/5"] = {"success": True, "data": None}
    rows, stats = _sync(fake, state)

    assert _tombstones(rows) == set()
    assert stats["failed"] == 1


def test_an_empty_listing_does_not_forget_records_still_in_pipedrive():
    fake, state = _company(), {}
    _sync(fake, state)

    fake.hidden.update({"organizations:1", "persons:2", "deals:3", "activities:4", "notes:5"})
    rows, _ = _sync(fake, state)

    assert _tombstones(rows) == set()


def test_paging_reads_every_page():
    fake = FakePipedrive(page_size=2)
    for i in range(1, 6):
        fake.add_deal(i, f"Deal {i}")
        fake.add_note(10 + i, f"Note {i}")

    rows, _ = _sync(fake, {}, include=("deals", "notes"))

    assert len(rows) == 10
    assert fake.count("/api/v2/deals") == 3 and fake.count("/v1/notes") == 3


def test_a_repeated_cursor_stops_the_run():
    fake = _company()
    fake.answers["/api/v2/deals"] = {
        "success": True,
        "data": [],
        "additional_data": {"next_cursor": "SAME"},
    }

    with pytest.raises(PipedriveAPIError, match="repeated a cursor"):
        _sync(fake, {}, include=("deals",))
    assert fake.count("/api/v2/deals") == 2


@pytest.mark.parametrize(
    "answer",
    [
        {"success": True, "data": []},
        {"success": True, "data": [], "additional_data": {}},
    ],
)
def test_a_listing_without_paging_info_raises_instead_of_looking_complete(answer):
    fake, state = _company(), {}
    _sync(fake, state)

    fake.answers["/api/v2/persons"] = answer
    with pytest.raises(PipedriveAPIError):
        _sync(fake, state)


def test_a_v2_listing_with_null_data_raises_instead_of_looking_empty():
    fake, state = _company(), {}
    _sync(fake, state)

    fake.answers["/api/v2/persons"] = {
        "success": True,
        "data": None,
        "additional_data": {"next_cursor": None},
    }
    with pytest.raises(PipedriveAPIError, match="no list"):
        _sync(fake, state)


def test_a_cursor_refused_after_a_cut_run_restarts_the_listing():
    fake, state = FakePipedrive(page_size=2), {}
    for i in range(1, 6):
        fake.add_deal(i, f"Deal {i}")
    fake.fail_from = ("/api/v2/deals", 2)
    fake.fail_with = PipedriveRateLimitedError("Pipedrive rate limit exceeded (HTTP 429)")
    _sync(fake, state, include=("deals",))

    fake.fail_after = fake.fail_from = None
    real_request = fake.request

    def expired_cursor(path, *, params=None, check_active=None):
        if path == "/api/v2/deals" and (params or {}).get("cursor") == "2" and not seen:
            seen.append(1)
            fake.calls.append((path, dict(params)))
            raise PipedriveAPIError("Pipedrive request failed: HTTP 400")
        return real_request(path, params=params, check_active=check_active)

    seen: list[int] = []
    fake.request = expired_cursor
    rows, stats = _sync(fake, state, include=("deals",))

    assert _ids(rows) == {"deal:3", "deal:4", "deal:5"}
    assert stats["failed"] == 0
    cursors = [p.get("cursor") for path, p in fake.calls if path == "/api/v2/deals"][-4:]
    assert cursors == ["2", None, "2", "4"]


def test_a_rate_limited_run_resumes_where_it_stopped():
    fake, state = FakePipedrive(page_size=2), {}
    for i in range(1, 6):
        fake.add_deal(i, f"Deal {i}")
    fake.fail_from = ("/api/v2/deals", 3)  # two deal pages, then 429
    fake.fail_with = PipedriveRateLimitedError("Pipedrive rate limit exceeded (HTTP 429)")

    rows, stats = _sync(fake, state, include=("deals",))
    assert _ids(rows) == {"deal:1", "deal:2", "deal:3", "deal:4"}
    assert stats["failed_rate_limit"] == 1

    fake.fail_after = fake.fail_from = None
    fake.calls.clear()
    rows, stats = _sync(fake, state, include=("deals",))
    assert _ids(rows) == {"deal:5"}
    assert stats["failed"] == 0
    # the cut run's pages are not read again
    assert [p.get("cursor") for path, p in fake.calls if path == "/api/v2/deals"] == ["4"]


def test_the_request_budget_cuts_a_run_and_nothing_is_forgotten_early():
    fake, state = FakePipedrive(page_size=1), {}
    for i in range(1, 4):
        fake.add_deal(i, f"Deal {i}")
    _sync(fake, state, include=("deals",))

    del fake.deals[1]
    # 8 lookup requests at one record a page, then both deal pages; the archived listing is cut
    rows, stats = _sync(fake, state, include=("deals",), max_requests=10)
    assert stats["failed_budget"] == 1 and stats["scanned"] == 2
    assert _tombstones(rows) == set()

    rows, _ = _sync(fake, state, include=("deals",))
    assert _tombstones(rows) == {"deal:1"}


def test_a_bad_token_raises_but_a_token_revoked_mid_run_keeps_progress():
    fake = _company()
    fake.fail_paths["/v1/users/me"] = PipedriveAuthError("Pipedrive rejected the API token")
    with pytest.raises(PipedriveAuthError):
        _sync(fake, {})

    fake = FakePipedrive(page_size=1)
    for i in range(1, 4):
        fake.add_deal(i, f"Deal {i}")
    fake.fail_from = ("/api/v2/deals", 2)
    fake.fail_with = PipedriveAuthError("Pipedrive rejected the API token")
    rows, stats = _sync(fake, {}, include=("deals",))
    assert _ids(rows) == {"deal:1"}
    assert stats["failed_auth"] == 1


def test_a_listing_the_user_cannot_read_raises():
    fake = _company()
    fake.fail_paths["/api/v2/activities"] = PipedriveAccessError("HTTP 403")

    with pytest.raises(PipedriveAccessError):
        _sync(fake, {})


def test_state_from_another_company_is_refused():
    state = {}
    _sync(_company(), state)

    with pytest.raises(PipedriveSourceError, match="another Pipedrive company"):
        _sync(FakePipedrive(company_id=2), state)


def test_check_active_is_asked_before_every_request():
    fake, asked = _company(), []
    _sync(fake, {}, check_active=lambda: asked.append(len(fake.calls)))

    assert len(asked) >= len(fake.calls)


def test_a_shuffled_listing_renders_the_same_rows():
    fake = _company()
    for i in range(10, 30):
        fake.add_note(i, f"Note {i}", deal_id=3)
    first = _by_id(_sync(fake, {})[0])

    items = list(fake.notes.items())
    random.Random(4).shuffle(items)
    fake.notes = dict(items)

    assert _by_id(_sync(fake, {})[0]) == first


def test_html_notes_become_readable_text():
    text = html_to_text(
        "<div>Hi&nbsp;team,</div><ul><li>SSO</li><li>Audit log</li></ul>"
        "<p>Budget: &euro;10k<br>Owner: Ada</p>"
    )

    assert text == "Hi team,\n\n- SSO\n- Audit log\n\nBudget: €10k\nOwner: Ada"
    # an escaped tag the user typed stays as written
    assert html_to_text("<p>Use &lt;b&gt; for bold</p>") == "Use <b> for bold"
    assert html_to_text("<p>&amp;lt; is an entity</p>") == "&lt; is an entity"
    assert html_to_text(None) == "" and html_to_text("  ") == ""
