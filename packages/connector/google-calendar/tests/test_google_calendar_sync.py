"""Sync state machine tests: _iter_rows against an in-memory Google Calendar.

No network, no LLM. Each test drives the fake through a real-world change
(edit, delete, cancelled occurrence, expired sync token, ...) and checks the
rows the connector yields and the state it keeps for the next run.
"""

from datetime import datetime

import pytest
from fake_calendar import FakeCalendar, HttpError

from cognee_community_connector_google_calendar.google_calendar import (
    _CalendarConfig,
    _iter_rows,
)

START = datetime(2026, 10, 12, 10, 0)
PRIMARY = "lay@gmail.com"


@pytest.fixture
def cal():
    return FakeCalendar()


def sync(fake, state, calendars=("primary",)):
    stats: dict = {}
    rows = list(_iter_rows(fake, _CalendarConfig(calendar_ids=tuple(calendars)), state, stats))
    upserts = {row["id"]: row for row in rows if not row["_deleted"]}
    deletes = sorted(row["id"] for row in rows if row["_deleted"])
    return upserts, deletes, stats


def rid(event_id, calendar="primary"):
    return f"gcal:{calendar}:{event_id}"


# ---------------------------------------------------------------------------
# First sync, paging and request shape
# ---------------------------------------------------------------------------
def test_first_sync_reads_every_page_and_saves_the_token(cal):
    ids = {cal.put_event("primary", f"Meeting {i}", START) for i in range(5)}
    state: dict = {}

    upserts, deletes, stats = sync(cal, state)

    assert set(upserts) == {rid(i) for i in ids}
    assert deletes == []
    assert stats["scanned"] == 5
    assert state["calendars"]["primary"]["sync_token"] == f"{PRIMARY}~5"
    assert sorted(state["calendars"]["primary"]["known"]) == sorted(ids)


def test_sync_token_rounds_keep_the_same_parameters_on_every_page(cal):
    cal.put_event("primary", "One", START)
    state: dict = {}
    sync(cal, state)
    for i in range(5):
        cal.put_event("primary", f"New {i}", START)
    cal.list_calls.clear()

    sync(cal, state)

    first, *pages = cal.list_calls
    assert first["syncToken"] == f"{PRIMARY}~1"
    assert pages, "the change round spans several pages"
    for page in pages:
        assert {k: v for k, v in page.items() if k != "pageToken"} == first
    assert "timeMin" not in first and "timeMax" not in first


def test_event_row_shape_and_content(cal):
    event_id = cal.put_event(
        "primary",
        "Battery review",
        START,
        location="Pune office",
        attendees=[
            {
                "email": "arjun@example.com",
                "displayName": "Arjun Rao",
                "responseStatus": "accepted",
            },
            {"email": "lay@gmail.com", "responseStatus": "tentative"},
        ],
        hangoutLink="https://meet.google.com/abc-defg-hij",
        description="<p>Battery lasts <b>55 minutes</b>.</p>",
    )

    upserts, _, _ = sync(cal, {})

    assert upserts[rid(event_id)] == {
        "id": rid(event_id),
        "title": "Battery review",
        "content": (
            "Calendar event: Battery review\n"
            "Calendar: Lay Patel\n"
            "When: 2026-10-12T10:00:00+05:30 (Asia/Kolkata) to "
            "2026-10-12T11:00:00+05:30 (Asia/Kolkata)\n"
            "Location: Pune office\n"
            "Organizer: Meera Joshi <meera@example.com>\n"
            "Attendees: Arjun Rao <arjun@example.com> (accepted), lay@gmail.com (tentative)\n"
            "Meeting link: https://meet.google.com/abc-defg-hij\n\n"
            "Battery lasts 55 minutes."
        ),
        "url": f"https://calendar.google.com/event?eid={event_id}",
        "_deleted": False,
    }


def test_all_day_event_and_omitted_attendees(cal):
    holiday = cal.put_all_day("primary", "Diwali", "2026-11-08")
    trip = cal.put_all_day("primary", "Trip", "2026-11-08", days=3)
    big = cal.put_event(
        "primary",
        "All hands",
        START,
        attendees=[{"email": "a@example.com", "responseStatus": "accepted"}],
        attendeesOmitted=True,
    )

    upserts, _, _ = sync(cal, {})

    # Google's end.date is exclusive, so the stored span ends the day before it.
    assert "When: all day, 2026-11-08\n" in upserts[rid(holiday)]["content"]
    assert "2026-11-09" not in upserts[rid(holiday)]["content"]
    assert "When: all day, 2026-11-08 to 2026-11-10\n" in upserts[rid(trip)]["content"]
    assert "and more not listed by Google" in upserts[rid(big)]["content"]


def test_working_location_events_are_skipped(cal):
    meeting = cal.put_event("primary", "Sync", START)
    cal.put_event("primary", "Office", START, eventType="workingLocation")

    upserts, _, stats = sync(cal, {})

    assert set(upserts) == {rid(meeting)}
    assert stats["skipped"] == 1


# ---------------------------------------------------------------------------
# Incremental rounds, edits and deletes
# ---------------------------------------------------------------------------
def test_resync_without_changes_yields_nothing(cal):
    cal.put_event("primary", "Sync", START)
    state: dict = {}
    sync(cal, state)

    upserts, deletes, _ = sync(cal, state)

    assert upserts == {} and deletes == []


def test_edit_reyields_the_same_event(cal):
    event_id = cal.put_event("primary", "Draft agenda", START)
    state: dict = {}
    sync(cal, state)

    cal.edit_event("primary", event_id, summary="Final agenda")
    upserts, deletes, _ = sync(cal, state)

    assert list(upserts) == [rid(event_id)]
    assert upserts[rid(event_id)]["title"] == "Final agenda"
    assert deletes == []


def test_deleted_event_is_forgotten(cal):
    keep = cal.put_event("primary", "Keep", START)
    gone = cal.put_event("primary", "Budget meeting", START)
    state: dict = {}
    sync(cal, state)

    cal.delete_event("primary", gone)
    upserts, deletes, stats = sync(cal, state)

    assert upserts == {}
    assert deletes == [rid(gone)]
    assert stats["deleted"] == 1
    assert state["calendars"]["primary"]["known"] == [keep]


# ---------------------------------------------------------------------------
# Recurring events
# ---------------------------------------------------------------------------
def test_series_is_one_document_with_its_rule(cal):
    series = cal.put_series("primary", "Falcon daily sync", START, "RRULE:FREQ=DAILY;COUNT=3")

    upserts, _, _ = sync(cal, {})

    assert list(upserts) == [rid(series)]
    assert "Repeats: RRULE:FREQ=DAILY;COUNT=3" in upserts[rid(series)]["content"]


def test_cancelled_single_occurrence_never_forgets_the_series(cal):
    series = cal.put_series("primary", "Falcon daily sync", START, "RRULE:FREQ=DAILY;COUNT=3")
    state: dict = {}
    sync(cal, state)

    cal.cancel_occurrence("primary", series, START.replace(day=13))
    _, deletes, _ = sync(cal, state)

    assert deletes == []
    assert state["calendars"]["primary"]["known"] == [series]


def test_changed_occurrence_is_its_own_document_and_can_be_cancelled(cal):
    series = cal.put_series("primary", "Falcon daily sync", START, "RRULE:FREQ=DAILY;COUNT=3")
    state: dict = {}
    sync(cal, state)

    moved = cal.edit_occurrence("primary", series, START.replace(day=13), location="Rooftop")
    upserts, _, _ = sync(cal, state)
    assert list(upserts) == [rid(moved)]
    assert f"Changed occurrence of series {series}" in upserts[rid(moved)]["content"]

    cal.cancel_occurrence("primary", series, START.replace(day=13))
    upserts, deletes, _ = sync(cal, state)
    assert deletes == [rid(moved)]
    assert state["calendars"]["primary"]["known"] == [series]


def test_deleting_a_whole_series_forgets_its_changed_occurrences(cal):
    series = cal.put_series("primary", "Falcon daily sync", START, "RRULE:FREQ=DAILY;COUNT=3")
    moved = cal.edit_occurrence("primary", series, START.replace(day=13), location="Rooftop")
    state: dict = {}
    sync(cal, state)

    cal.delete_event("primary", series)
    _, deletes, _ = sync(cal, state)

    assert deletes == sorted([rid(series), rid(moved)])


# ---------------------------------------------------------------------------
# Expired tokens, missing calendars and errors
# ---------------------------------------------------------------------------
def test_expired_sync_token_resyncs_like_a_snapshot(cal):
    keep = cal.put_event("primary", "Keep", START)
    gone = cal.put_event("primary", "Gone", START)
    state: dict = {}
    sync(cal, state)

    cal.delete_event("primary", gone)
    cal.expired_tokens.add(state["calendars"]["primary"]["sync_token"])
    upserts, deletes, _ = sync(cal, state)

    assert deletes == [rid(gone)]
    assert set(upserts) == {rid(keep)}  # a full listing re-yields every event


def test_deleted_calendar_forgets_its_events(cal):
    cal.add_calendar("team@group.calendar.google.com", "Team")
    team = cal.put_event("team@group.calendar.google.com", "Team lunch", START)
    state: dict = {}
    calendars = ("primary", "team@group.calendar.google.com")
    sync(cal, state, calendars)

    cal.delete_calendar("team@group.calendar.google.com")
    _upserts, deletes, _ = sync(cal, state, calendars)

    assert deletes == [rid(team, "team@group.calendar.google.com")]
    assert set(state["calendars"]) == {"primary"}


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_errors_raise_and_never_delete(cal, status):
    cal.put_event("primary", "Keep", START)
    state: dict = {}
    sync(cal, state)
    before = {"calendars": {k: dict(v) for k, v in state["calendars"].items()}}

    cal.fail_next.append(status)
    with pytest.raises(HttpError):
        sync(cal, state)

    assert state == before


def test_error_on_a_later_page_saves_nothing(cal):
    cal.put_event("primary", "One", START)
    state: dict = {}
    sync(cal, state)
    for i in range(5):
        cal.put_event("primary", f"New {i}", START)
    before = {"calendars": {k: dict(v) for k, v in state["calendars"].items()}}

    original = cal._list_events

    def fail_on_second_page(params):
        if "pageToken" in params:
            raise HttpError(503)
        return original(params)

    cal._list_events = fail_on_second_page
    with pytest.raises(HttpError):
        sync(cal, state)

    assert state == before


# ---------------------------------------------------------------------------
# Several calendars
# ---------------------------------------------------------------------------
def test_events_in_two_calendars_keep_separate_ids_and_names(cal):
    cal.add_calendar("team@group.calendar.google.com", "Team")
    shared_id = "invite1"
    cal.put_event("primary", "Launch", START, id=shared_id)
    cal.put_event("team@group.calendar.google.com", "Launch", START, id=shared_id)

    upserts, _, _ = sync(cal, {}, ("primary", "team@group.calendar.google.com"))

    assert set(upserts) == {rid(shared_id), rid(shared_id, "team@group.calendar.google.com")}
    assert "Calendar: Team" in upserts[rid(shared_id, "team@group.calendar.google.com")]["content"]


def test_dropping_a_calendar_from_the_config_forgets_its_events(cal):
    cal.add_calendar("team@group.calendar.google.com", "Team")
    team = cal.put_event("team@group.calendar.google.com", "Team lunch", START)
    state: dict = {}
    sync(cal, state, ("primary", "team@group.calendar.google.com"))

    _, deletes, _ = sync(cal, state, ("primary",))

    assert deletes == [rid(team, "team@group.calendar.google.com")]
    assert set(state["calendars"]) == {"primary"}
