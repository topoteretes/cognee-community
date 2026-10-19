"""Sync state machine tests: _iter_rows against an in-memory Google Meet and Drive.

No network, no LLM. Each test drives the fakes through a real-world change
(a meeting ends, a transcript arrives late, a Doc is deleted, Meet expires a
record, ...) and checks the rows the connector yields and the state it keeps
for the next run.
"""

import logging
from datetime import datetime, timedelta, timezone

import pytest
from fake_meet import FakeDrive, FakeMeet, HttpError, install_google_errors

from cognee_community_connector_google_meet.google_meet import (
    GoogleMeetError,
    MeetServices,
    _iter_rows,
    _list_all,
    _MeetConfig,
    _parse_time,
)

T0 = datetime(2026, 10, 17, 10, 0)
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)


def at(minutes: int, seconds: int = 0) -> datetime:
    return T0 + timedelta(minutes=minutes, seconds=seconds)


@pytest.fixture(autouse=True)
def _google_errors(monkeypatch):
    install_google_errors(monkeypatch)


@pytest.fixture
def drive():
    return FakeDrive()


@pytest.fixture
def meet(drive):
    return FakeMeet(drive=drive)


def sync(meet, drive, state, now=NOW, check_active=None, **config):
    stats: dict = {}
    services = MeetServices(meet=meet, drive=drive)
    rows = list(_iter_rows(services, _MeetConfig(**config), state, stats, check_active, now=now))
    upserts = {row["id"]: row for row in rows if not row["_deleted"]}
    deletes = sorted(row["id"] for row in rows if row["_deleted"])
    return upserts, deletes, stats


def conf_id(conference_id):
    return f"gmeet:conference:{conference_id}"


def transcript_id(transcript_name):
    parts = transcript_name.split("/")
    return f"gmeet:transcript:{parts[1]}/{parts[3]}"


def meeting(meet, space=None, start=0, end=45):
    """A finished conference with one participant."""
    space = space or meet.add_space()
    conference = meet.start_conference(space, at(start))
    meet.add_participant(conference, "Meera Joshi", [(at(start), at(end))])
    meet.end_conference(conference, at(end))
    return conference


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def test_conference_row_shape_and_content(meet, drive):
    space = meet.add_space("abc-mnop-xyz")
    conference = meet.start_conference(space, at(2))
    # Left and rejoined: two sessions. Meet returns them newest first.
    meet.add_participant(conference, "Meera Joshi", [(at(2), at(20)), (at(25), at(45))])
    meet.add_participant(conference, "Ravi", [(at(5), at(45))], kind="anonymousUser")
    meet.add_participant(conference, "+91 *** *** 1234", [(at(10), at(30))], kind="phoneUser")
    meet.end_conference(conference, at(45, 12))

    upserts, _, stats = sync(meet, drive, {})

    assert upserts[conf_id(conference)] == {
        "id": conf_id(conference),
        "title": "Meet abc-mnop-xyz, 2026-10-17",
        "content": (
            "Google Meet conference\n"
            "Meeting link: https://meet.google.com/abc-mnop-xyz\n"
            "Meeting code: abc-mnop-xyz\n"
            "Started: 2026-10-17T10:02:00Z\n"
            "Ended: 2026-10-17T10:45:12Z\n"
            "Participants (times in UTC):\n"
            "- Meera Joshi (signed in): 10:02 to 10:20, 10:25 to 10:45\n"
            "- Ravi (guest): 10:05 to 10:45\n"
            "- +91 *** *** 1234 (phone): 10:10 to 10:30\n"
            "Transcript: no\n"
            "Recording: no"
        ),
        "url": "https://meet.google.com/abc-mnop-xyz",
        "_deleted": False,
    }
    assert stats == {
        "conferences": 1,
        "transcripts": 0,
        "deleted": 0,
        "pending": 0,
        "failed": 0,
        "failed_auth": 0,
        "failed_rate_limit": 0,
        "failed_conference": 0,
        "failed_transcript_check": 0,
        "failed_conference_list": 0,
    }


def test_meeting_link_line_matches_the_google_calendar_connector():
    # The Calendar connector writes f"Meeting link: {event['hangoutLink']}", and
    # hangoutLink is the same URL as the space's meetingUri. Keeping the two
    # lines identical lets cognify join a conference to its calendar event.
    meet = FakeMeet()
    conference = meeting(meet, meet.add_space("jtc-ycsq-xpu"))

    upserts, _, _ = sync(meet, None, {}, include_transcripts=False)

    lines = upserts[conf_id(conference)]["content"].splitlines()
    assert "Meeting link: https://meet.google.com/jtc-ycsq-xpu" in lines


def test_recording_is_noted_never_downloaded(meet, drive):
    conference = meeting(meet)
    meet.add_recording(conference)

    upserts, _, _ = sync(meet, drive, {})

    assert "Recording: yes" in upserts[conf_id(conference)]["content"]
    assert meet.calls_to("recordings") == [
        {"parent": f"conferenceRecords/{conference}", "pageSize": 1}
    ]


def test_transcript_row_maps_speakers_and_keeps_order(meet, drive):
    space = meet.add_space("abc-mnop-xyz")
    conference = meet.start_conference(space, at(0))
    meera = meet.add_participant(conference, "Meera Joshi", [(at(0), at(30))])
    ravi = meet.add_participant(conference, "Ravi", [(at(1), at(30))], kind="anonymousUser")
    meet.end_conference(conference, at(30))
    transcript = meet.add_transcript(
        conference,
        [
            (meera, "The drone battery lasts 55 minutes.", at(3, 12)),
            (ravi, " Can we test it on Friday? ", at(3, 20)),
            ("conferenceRecords/x/participants/gone", "Hello?", at(4)),
        ],
    )
    doc = transcript.split("/")[-1]

    upserts, _, stats = sync(meet, drive, {})

    assert upserts[transcript_id(transcript)] == {
        "id": transcript_id(transcript),
        "title": "Meet transcript, abc-mnop-xyz, 2026-10-17",
        "content": (
            "Google Meet transcript\n"
            "Meeting link: https://meet.google.com/abc-mnop-xyz\n"
            "Meeting code: abc-mnop-xyz\n"
            "Conference started: 2026-10-17T10:00:00Z\n"
            "Transcript (times in UTC):\n"
            "[10:03:12] Meera Joshi: The drone battery lasts 55 minutes.\n"
            "[10:03:20] Ravi: Can we test it on Friday?\n"
            "[10:04:00] Unknown speaker: Hello?"
        ),
        "url": f"https://docs.google.com/document/d/doc-{doc}/view",
        "_deleted": False,
    }
    assert "Transcript: yes" in upserts[conf_id(conference)]["content"]
    assert stats["transcripts"] == 1


def test_participant_without_sessions_falls_back_to_join_and_leave_times(meet, drive):
    conference = meeting(meet)
    meet.participants[conference][0]["_sessions"] = []

    upserts, _, _ = sync(meet, drive, {})

    assert "- Meera Joshi (signed in): 10:00 to 10:45" in upserts[conf_id(conference)]["content"]


# ---------------------------------------------------------------------------
# Paging and listing
# ---------------------------------------------------------------------------
def test_every_list_call_reads_all_pages(meet, drive):
    space = meet.add_space()
    conferences = [meeting(meet, space, start=i * 60, end=i * 60 + 30) for i in range(3)]
    big = conferences[0]
    for name in ("Arjun", "Bela"):
        meet.add_participant(big, name, [(at(1), at(5)), (at(6), at(9)), (at(10), at(20))])
    speaker = meet.participants[big][0]["name"]
    first = meet.add_transcript(big, [(speaker, f"line {i}", at(1, i)) for i in range(5)])
    for _ in range(2):
        meet.add_transcript(big)

    upserts, _, _ = sync(meet, drive, {})

    assert {conf_id(c) for c in conferences} <= set(upserts)
    content = upserts[conf_id(big)]["content"]
    assert "- Arjun (signed in): 10:01 to 10:05, 10:06 to 10:09, 10:10 to 10:20" in content
    assert "- Bela (signed in)" in content
    assert content.count("(signed in)") == 3
    assert upserts[transcript_id(first)]["content"].count("Meera Joshi: line") == 5
    assert sum(1 for row_id in upserts if row_id.startswith("gmeet:transcript:")) == 3
    for method in ("records", "participants", "sessions", "transcripts", "entries"):
        assert any("pageToken" in p for p in meet.calls_to(method)), f"{method} paged"


def test_each_list_asks_for_the_largest_page(meet, drive):
    conference = meeting(meet)
    meet.add_transcript(conference, [(meet.participants[conference][0]["name"], "hi", at(1))])

    sync(meet, drive, {})

    assert meet.calls_to("records")[0]["pageSize"] == 100
    assert meet.calls_to("participants")[0]["pageSize"] == 250
    assert meet.calls_to("sessions")[0]["pageSize"] == 250
    assert meet.calls_to("transcripts")[0]["pageSize"] == 100
    assert meet.calls_to("entries")[0]["pageSize"] == 100


def test_conferences_sharing_a_link_read_the_space_once(meet, drive):
    space = meet.add_space()
    meeting(meet, space, start=0, end=10)
    meeting(meet, space, start=20, end=30)

    upserts, _, _ = sync(meet, drive, {})

    assert len(upserts) == 2
    assert len(meet.calls_to("spaces.get")) == 1


def test_a_conference_with_nobody_and_nothing_still_syncs(meet, drive):
    conference = meet.start_conference(meet.add_space(), at(0))
    meet.end_conference(conference, at(1))

    upserts, _, _ = sync(meet, drive, {})

    assert "Participants: none recorded" in upserts[conf_id(conference)]["content"]


# ---------------------------------------------------------------------------
# Incremental runs
# ---------------------------------------------------------------------------
def test_second_run_without_changes_yields_nothing(meet, drive):
    meeting(meet)
    state: dict = {}
    sync(meet, drive, state)

    upserts, deletes, _ = sync(meet, drive, state)

    assert upserts == {} and deletes == []


def test_later_runs_list_from_an_hour_before_the_newest_conference(meet, drive):
    old = meeting(meet, start=0, end=30)
    state: dict = {}
    sync(meet, drive, state)
    assert state["since"] == "2026-10-17T10:00:00Z"
    assert "filter" not in meet.calls_to("records")[0], "the first run lists everything"

    # Starts inside the one-hour overlap: listed again, but `done` stops a repeat.
    new = meeting(meet, start=40, end=50)
    meet.calls.clear()
    upserts, _, _ = sync(meet, drive, state)

    assert meet.calls_to("records")[0]["filter"] == 'start_time>="2026-10-17T09:00:00Z"'
    assert set(upserts) == {conf_id(new)}
    assert conf_id(old) not in upserts
    assert state["since"] == "2026-10-17T10:40:00Z"
    assert set(state["done"]) == {old, new}


def test_ongoing_conference_waits_and_is_synced_once_it_ends(meet, drive):
    space = meet.add_space()
    conference = meet.start_conference(space, at(0))
    meet.add_participant(conference, "Meera Joshi", [(at(0), None)])
    state: dict = {}

    upserts, _, stats = sync(meet, drive, state)
    assert upserts == {}
    assert state["pending"] == {conference: ""}
    assert stats["pending"] == 1

    meet.participants[conference][0]["_sessions"][0]["endTime"] = "2026-10-17T10:30:00.000000Z"
    meet.participants[conference][0]["latestEndTime"] = "2026-10-17T10:30:00.000000Z"
    meet.end_conference(conference, at(30))
    upserts, _, _ = sync(meet, drive, state)

    assert set(upserts) == {conf_id(conference)}
    assert state["pending"] == {}
    assert conference in state["done"]


def test_cursor_never_moves_backwards(meet, drive):
    space = meet.add_space()
    long_call = meet.start_conference(space, at(0))
    meet.add_participant(long_call, "Meera Joshi", [(at(0), at(200))])
    later = meeting(meet, space, start=60, end=70)
    state: dict = {}
    sync(meet, drive, state)
    assert state["since"] == "2026-10-17T11:00:00Z"

    # The long call ends; it started earlier, but the cursor must not go back.
    meet.end_conference(long_call, at(200))
    upserts, _, _ = sync(meet, drive, state)

    assert set(upserts) == {conf_id(long_call)}
    assert state["since"] == "2026-10-17T11:00:00Z"
    assert set(state["done"]) == {long_call, later}


def test_pending_conference_older_than_the_window_is_read_by_id(meet, drive):
    space = meet.add_space()
    long_call = meet.start_conference(space, at(0))
    state: dict = {}
    sync(meet, drive, state)
    for i in range(1, 4):  # the cursor moves hours past the long call
        meeting(meet, space, start=i * 120, end=i * 120 + 10)
    sync(meet, drive, state)
    meet.end_conference(long_call, at(500))
    meet.calls.clear()

    upserts, _, _ = sync(meet, drive, state)

    assert set(upserts) == {conf_id(long_call)}
    assert meet.calls_to("records.get") == [{"name": f"conferenceRecords/{long_call}"}]


# ---------------------------------------------------------------------------
# Late and multiple transcripts
# ---------------------------------------------------------------------------
def test_late_transcript_is_picked_up_on_a_later_run(meet, drive):
    conference = meeting(meet)
    transcript = meet.add_transcript(conference, state="ENDED")
    state: dict = {}

    upserts, _, _ = sync(meet, drive, state)
    assert set(upserts) == {conf_id(conference)}
    assert "Transcript: being generated" in upserts[conf_id(conference)]["content"]
    assert conference in state["pending"] and conference not in state["done"]

    meet.set_transcript_state(transcript, "FILE_GENERATED")
    upserts, _, _ = sync(meet, drive, state)

    assert set(upserts) == {conf_id(conference), transcript_id(transcript)}
    assert "Transcript: yes" in upserts[conf_id(conference)]["content"]
    assert conference in state["done"] and state["pending"] == {}


def test_several_transcripts_in_one_conference_and_one_still_running(meet, drive):
    conference = meeting(meet)
    first = meet.add_transcript(conference)
    second = meet.add_transcript(conference, state="STARTED")
    state: dict = {}

    upserts, _, _ = sync(meet, drive, state)
    assert transcript_id(first) in upserts
    assert conference in state["pending"]

    meet.set_transcript_state(second, "FILE_GENERATED")
    meet.calls.clear()
    upserts, _, _ = sync(meet, drive, state)

    assert transcript_id(second) in upserts
    assert transcript_id(first) not in upserts, "a finished transcript is not fetched again"
    assert len(meet.calls_to("entries")) == 1
    assert set(state["transcripts"]) == {transcript_id(first), transcript_id(second)}


def test_empty_transcript_list_is_normal(meet, drive):
    # Personal accounts cannot transcribe: Meet answers {} with no list key.
    conference = meeting(meet)
    state: dict = {}

    upserts, _, _ = sync(meet, drive, state)

    assert "Transcript: no" in upserts[conf_id(conference)]["content"]
    assert state["transcripts"] == {}


# ---------------------------------------------------------------------------
# Forget-on-delete through Drive
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("remove", ["trash", "delete"])
def test_deleted_transcript_doc_is_forgotten_and_the_conference_stays(meet, drive, remove):
    conference = meeting(meet)
    transcript = meet.add_transcript(conference, doc_id="doc-falcon")
    state: dict = {}
    sync(meet, drive, state)

    getattr(drive, remove)("doc-falcon")
    upserts, deletes, stats = sync(meet, drive, state)

    assert deletes == [transcript_id(transcript)]
    assert conf_id(conference) not in deletes
    assert upserts == {}
    assert state["transcripts"] == {}
    assert stats["deleted"] == 1


def test_deleted_doc_of_a_pending_conference_is_not_added_back(meet, drive):
    conference = meeting(meet)
    meet.add_transcript(conference, doc_id="doc-falcon")
    meet.add_transcript(conference, state="ENDED")  # keeps the conference pending
    state: dict = {}
    sync(meet, drive, state)

    drive.trash("doc-falcon")
    _, deletes, _ = sync(meet, drive, state)
    assert len(deletes) == 1

    upserts, deletes, _ = sync(meet, drive, state)
    assert deletes == []
    assert not any(row_id.startswith("gmeet:transcript:") for row_id in upserts)
    assert state["transcripts"] == {}


def test_doc_deleted_before_the_first_sync_is_never_stored(meet, drive):
    conference = meeting(meet)
    meet.add_transcript(conference, doc_id="doc-falcon")
    drive.delete("doc-falcon")

    upserts, deletes, _ = sync(meet, drive, {})

    assert set(upserts) == {conf_id(conference)}
    assert deletes == []
    assert "Transcript: no" in upserts[conf_id(conference)]["content"]


def snapshot(state):
    return {k: (dict(v) if isinstance(v, dict) else v) for k, v in state.items()}


@pytest.mark.parametrize("status", [403, 500])
def test_a_doc_that_cannot_be_checked_stays_watched_and_the_rest_syncs(meet, drive, status):
    old = meeting(meet)
    transcript = meet.add_transcript(old, doc_id="doc-falcon")
    state: dict = {}
    sync(meet, drive, state)
    new = meeting(meet, start=60, end=70)

    drive.errors["doc-falcon"] = status
    upserts, deletes, stats = sync(meet, drive, state)

    assert deletes == [], "errors never delete"
    assert set(upserts) == {conf_id(new)}
    assert transcript_id(transcript) in state["transcripts"]
    assert stats["failed"] == 1 and stats["failed_transcript_check"] == 1

    del drive.errors["doc-falcon"]
    drive.trash("doc-falcon")
    _, deletes, _ = sync(meet, drive, state)
    assert deletes == [transcript_id(transcript)], "checked again on the next run"


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_a_drive_error_with_nothing_else_finished_raises_and_changes_nothing(meet, drive, status):
    conference = meeting(meet)
    meet.add_transcript(conference)
    state: dict = {}
    sync(meet, drive, state)
    before = snapshot(state)

    drive.fail_next.append(status)
    with pytest.raises(GoogleMeetError, match=f"HTTP {status}"):
        sync(meet, drive, state)

    assert state == before


def test_without_drive_checks_drive_is_never_called(meet):
    conference = meeting(meet)
    transcript = meet.add_transcript(conference)
    state: dict = {}

    upserts, _, _ = sync(meet, None, state, check_drive_deletes=False)
    sync(meet, None, state, check_drive_deletes=False)

    assert transcript_id(transcript) in upserts
    assert transcript_id(transcript) in state["transcripts"]


def test_turning_transcripts_off_forgets_the_synced_ones(meet, drive):
    conference = meeting(meet)
    transcript = meet.add_transcript(conference)
    state: dict = {}
    sync(meet, drive, state)
    meeting(meet, start=60, end=70)
    meet.calls.clear()
    drive.calls.clear()

    upserts, deletes, _ = sync(meet, drive, state, include_transcripts=False)

    assert deletes == [transcript_id(transcript)]
    assert meet.calls_to("transcripts") == []
    assert drive.calls == []
    assert all("Transcript:" not in row["content"] for row in upserts.values())
    assert state["transcripts"] == {}


# ---------------------------------------------------------------------------
# Expiry is not a delete
# ---------------------------------------------------------------------------
def test_expired_pending_conference_is_dropped_without_deleting(meet, drive):
    conference = meeting(meet)
    meet.add_transcript(conference, state="ENDED")
    state: dict = {}
    sync(meet, drive, state)
    meeting(meet, start=120, end=130)  # moves the cursor past the first one
    sync(meet, drive, state)

    meet.expire(conference)
    upserts, deletes, _ = sync(meet, drive, state)

    assert deletes == [] and upserts == {}
    assert conference not in state["pending"]


def test_done_entries_are_pruned_after_expiry_without_deleting(meet, drive):
    conference = meeting(meet)
    transcript = meet.add_transcript(conference)
    state: dict = {}
    sync(meet, drive, state)

    later = NOW + timedelta(days=31)
    meet.expire(conference)
    _, deletes, _ = sync(meet, drive, state, now=later)

    assert deletes == []
    assert state["done"] == {}
    # The Doc outlives Meet's 30 days, so its transcript is still watched.
    assert transcript_id(transcript) in state["transcripts"]


# ---------------------------------------------------------------------------
# Errors never delete, and one bad conference does not block the others
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("status", "key"),
    [(401, "failed_auth"), (429, "failed_rate_limit"), (500, "failed_conference_list")],
)
def test_a_failing_conference_list_raises_and_is_counted(meet, drive, status, key):
    meeting(meet)
    state: dict = {}
    sync(meet, drive, state)
    meeting(meet, start=60, end=70)
    before = snapshot(state)
    stats: dict = {}

    meet.fail_next.append(status)
    with pytest.raises(GoogleMeetError, match=f"HTTP {status}"):
        list(_iter_rows(MeetServices(meet, drive), _MeetConfig(), state, stats, now=NOW))

    assert state == before
    assert stats["failed"] == 1 and stats[key] == 1


def test_error_on_a_later_page_saves_nothing(meet, drive):
    space = meet.add_space()
    for i in range(5):
        meeting(meet, space, start=i * 10, end=i * 10 + 5)
    state: dict = {}
    original = meet._list

    def fail_on_second_page(method, key, items_fn, params):
        if params.get("pageToken"):
            raise HttpError(503)
        return original(method, key, items_fn, params)

    meet._list = fail_on_second_page
    with pytest.raises(GoogleMeetError, match="HTTP 503"):
        sync(meet, drive, state)

    assert state == {}


def test_a_failing_conference_stays_pending_while_the_others_finish(meet, drive):
    space = meet.add_space()
    broken = meeting(meet, space, start=0, end=10)
    fine = meeting(meet, space, start=120, end=130)
    state: dict = {}

    meet.errors[broken] = 500
    upserts, deletes, stats = sync(meet, drive, state)

    assert set(upserts) == {conf_id(fine)}
    assert deletes == []
    assert stats["conferences"] == 1 and stats["failed_conference"] == 1
    # The cursor moved past it, so only the pending list brings it back.
    assert state["since"] == "2026-10-17T12:00:00Z"
    assert broken in state["pending"] and broken not in state["done"]

    del meet.errors[broken]
    upserts, _, _ = sync(meet, drive, state)
    assert set(upserts) == {conf_id(broken)}
    assert state["pending"] == {}


def test_a_failing_pending_conference_stays_pending(meet, drive):
    conference = meeting(meet)
    meet.add_transcript(conference, state="ENDED")
    state: dict = {}
    sync(meet, drive, state)
    other = meeting(meet, start=120, end=130)  # moves the cursor past the first one
    sync(meet, drive, state)
    newer = meeting(meet, start=240, end=250)

    meet.errors[conference] = 503
    upserts, deletes, _ = sync(meet, drive, state)

    assert set(upserts) == {conf_id(newer)}
    assert deletes == []
    assert conference in state["pending"]
    assert other in state["done"]


def test_every_conference_failing_raises_and_saves_nothing(meet, drive):
    space = meet.add_space()
    first = meeting(meet, space, start=0, end=10)
    second = meeting(meet, space, start=20, end=30)
    meet.errors[first] = 500
    meet.errors[second] = 500
    state: dict = {}

    with pytest.raises(GoogleMeetError, match="HTTP 500"):
        sync(meet, drive, state)

    assert state == {}


@pytest.mark.parametrize(
    ("status", "reason", "key"),
    [
        (401, None, "failed_auth"),
        (429, None, "failed_rate_limit"),
        (403, "rateLimitExceeded", "failed_rate_limit"),
        (403, "userRateLimitExceeded", "failed_rate_limit"),
    ],
)
def test_auth_and_rate_limit_end_the_run_but_keep_what_finished(meet, drive, status, reason, key):
    space = meet.add_space()
    first = meeting(meet, space, start=0, end=10)
    second = meeting(meet, space, start=20, end=30)
    third = meeting(meet, space, start=40, end=50)
    meet.errors[second] = (status, reason) if reason else status
    state: dict = {}

    upserts, _, stats = sync(meet, drive, state)

    assert set(upserts) == {conf_id(first)}
    assert stats["failed"] == 1 and stats[key] == 1
    assert set(state["done"]) == {first}
    assert set(state["pending"]) == {second, third}
    # Every later call would fail the same way, so the third one is not tried.
    assert not any(third in str(params) for _method, params in meet.calls)


def test_a_403_without_a_rate_limit_reason_fails_only_its_conference(meet, drive):
    # Lost access to one conference says nothing about the others.
    space = meet.add_space()
    broken = meeting(meet, space, start=0, end=10)
    fine = meeting(meet, space, start=20, end=30)
    meet.errors[broken] = (403, "forbidden")

    upserts, _, stats = sync(meet, drive, {})

    assert set(upserts) == {conf_id(fine)}
    assert stats["failed_conference"] == 1 and stats["failed_rate_limit"] == 0


def test_meet_404_on_a_listed_conference_is_a_failure_not_an_expiry(meet, drive):
    # Only a pending record's 404 means "expired"; anywhere else it is an error.
    meeting(meet)
    state: dict = {}

    def gone(params):
        raise HttpError(404)

    meet._participants_of = gone
    with pytest.raises(GoogleMeetError, match="HTTP 404"):
        sync(meet, drive, state)

    assert state == {}


def test_stats_start_every_run_with_zero_failures(meet, drive):
    meeting(meet)
    stats: dict = {"failed_auth": 1, "conferences": 9}

    list(_iter_rows(MeetServices(meet, drive), _MeetConfig(), {}, stats, now=NOW))

    assert stats == {
        "conferences": 1,
        "transcripts": 0,
        "deleted": 0,
        "pending": 0,
        "failed": 0,
        "failed_auth": 0,
        "failed_rate_limit": 0,
        "failed_conference": 0,
        "failed_transcript_check": 0,
        "failed_conference_list": 0,
    }


# ---------------------------------------------------------------------------
# Authorization checkpoint
# ---------------------------------------------------------------------------
class _RevokedError(Exception):
    pass


def test_check_active_runs_before_every_api_call_not_only_between_rows(meet, drive):
    space = meet.add_space()
    for i in range(3):
        meeting(meet, space, start=i * 10, end=i * 10 + 5)
    state: dict = {}

    def check_active():
        # The connection is revoked after the first two requests.
        if len(meet.calls) >= 2:
            raise _RevokedError

    with pytest.raises(_RevokedError):
        sync(meet, drive, state, check_active=check_active)

    assert len(meet.calls) == 2, "no request after the revocation"
    assert state == {}


def test_check_active_also_guards_drive_calls(meet, drive):
    conference = meeting(meet)
    meet.add_transcript(conference)
    state: dict = {}
    sync(meet, drive, state)
    drive.calls.clear()

    def check_active():
        if meet.calls_to("records"):  # revoked right after the conference list
            raise _RevokedError

    meet.calls.clear()
    with pytest.raises(_RevokedError):
        sync(meet, drive, state, check_active=check_active)

    assert drive.calls == []


def test_check_active_is_not_swallowed_by_the_per_conference_error_handling(meet, drive):
    space = meet.add_space()
    meeting(meet, space, start=0, end=10)
    meeting(meet, space, start=20, end=30)
    checks: list[int] = []

    def check_active():
        checks.append(1)
        if len(checks) == 4:  # inside the first conference
            raise _RevokedError

    with pytest.raises(_RevokedError):
        sync(meet, drive, {}, check_active=check_active)


# ---------------------------------------------------------------------------
# Stable content and privacy
# ---------------------------------------------------------------------------
def test_participant_order_from_google_does_not_change_the_content(drive):
    def build(names):
        meet = FakeMeet(drive=drive)
        conference = meet.start_conference(meet.add_space(), at(0))
        for name in names:
            # Everyone joined at the same second: only the name decides the order.
            meet.add_participant(conference, name, [(at(0), at(30))])
        meet.end_conference(conference, at(30))
        upserts, _, _ = sync(meet, drive, {})
        return upserts[conf_id(conference)]["content"]

    assert build(["Zed", "Ada", "Meera"]) == build(["Meera", "Zed", "Ada"])


def test_timestamps_with_nanoseconds_and_offsets_are_read_as_utc():
    assert _parse_time("2026-10-17T10:02:00.123456789Z") == datetime(
        2026, 10, 17, 10, 2, 0, 123456, tzinfo=timezone.utc
    )
    assert _parse_time("2026-10-17T15:32:00+05:30") == datetime(
        2026, 10, 17, 10, 2, tzinfo=timezone.utc
    )


def test_no_ids_or_names_reach_logs_or_error_text(meet, drive, caplog):
    caplog.set_level(logging.DEBUG)
    space = meet.add_space("sec-rett-xyz")
    secret = meet.start_conference(space, at(0))
    meet.add_participant(secret, "Meera Joshi", [(at(0), at(10))])
    meet.end_conference(secret, at(10))
    meet.add_transcript(secret, doc_id="doc-secret-plan")
    state: dict = {}
    sync(meet, drive, state)

    # Every path that logs: a Doc that cannot be checked, a failing conference,
    # then a run where everything fails.
    meeting(meet, start=60, end=70)
    drive.errors["doc-secret-plan"] = 500
    sync(meet, drive, state)
    other = meeting(meet, start=120, end=130)
    meet.errors[other] = 500
    with pytest.raises(GoogleMeetError) as error:
        sync(meet, drive, state)

    assert caplog.text, "the paths above log"
    for secret_text in (secret, other, "doc-secret-plan", space, "sec-rett-xyz", "Meera"):
        assert secret_text not in caplog.text
        assert secret_text not in str(error.value)
    # The cause is chained for debugging and carries the same promise.
    chain = error.value.__cause__
    assert chain is not None and other not in str(chain) and "doc-secret-plan" not in str(chain)


def test_a_repeated_page_token_raises_instead_of_looping():
    calls: list = []

    class _Stuck:
        def execute(self, num_retries=0):
            calls.append(1)
            return {"conferenceRecords": [{"name": "conferenceRecords/x"}], "nextPageToken": "same"}

    with pytest.raises(GoogleMeetError, match="same page token"):
        _list_all(lambda **_params: _Stuck(), "conferenceRecords", None)

    assert len(calls) == 2
