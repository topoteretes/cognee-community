"""Sync state machine tests: _iter_rows against an in-memory Microsoft Graph.

No network, no LLM. The fake Graph is served over HTTP with ``responses``, so
dlt's real RESTClient, the nextLink paginator and its retries all run.
"""

import copy
import json
import logging
from datetime import timedelta

import pytest
import requests
import responses
from fake_graph import BASE, NOW, FakeGraph

import cognee_community_connector_outlook.outlook as outlook_module
from cognee_community_connector_outlook.graph_auth import GraphAuth, GraphTokenProvider
from cognee_community_connector_outlook.outlook import (
    OutlookError,
    OutlookFolderNotFoundError,
    _iter_rows,
    _OutlookConfig,
    build_graph_client,
)


@pytest.fixture
def graph():
    fake = FakeGraph()
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        fake.install(mock)
        yield fake


def make_config(**overrides) -> _OutlookConfig:
    values = {
        "include_mail": True,
        "mail_folders": ("inbox", "sentitems"),
        "include_mail_bodies": True,
        "mail_received_after": None,
        "include_calendar": True,
        "calendar_days_back": 30,
        "calendar_days_ahead": 30,
        "calendar_window_refresh_days": 7,
        "time_zone": "UTC",
    }
    values.update(overrides)
    return _OutlookConfig(**values)


def sync(state, config=None, now=NOW, check_active=None, stats=None):
    client = build_graph_client(
        GraphAuth(GraphTokenProvider(access_token="test-token"), BASE), BASE
    )
    stats = {} if stats is None else stats
    rows = list(
        _iter_rows(
            client, config or make_config(), state, stats, now=now, check_active=check_active
        )
    )
    upserts = {row["id"]: row for row in rows if not row["_deleted"]}
    deletes = sorted(row["id"] for row in rows if row["_deleted"])
    return upserts, deletes, stats


@pytest.fixture
def no_retry_wait(monkeypatch):
    """The Graph session retries 429s and lost connections with real back-off
    sleeps; keep the retries but skip the waiting."""
    monkeypatch.setattr(outlook_module, "_sleep", lambda _seconds: None)


def mail(message_id):
    return "mail:" + message_id


def event(event_id):
    return "event:" + event_id


# ---------------------------------------------------------------------------
# Mail: first sync, pagination, request shape
# ---------------------------------------------------------------------------
def test_first_sync_reads_every_page_of_every_folder(graph):
    inbox = {graph.put_message("inbox", f"Inbox {i}") for i in range(5)}
    sent = graph.put_message("sentitems", "Sent one")
    graph.put_message("archive", "Not synced")
    state: dict = {}

    upserts, deletes, stats = sync(state, make_config(include_calendar=False))

    assert set(upserts) == {mail(i) for i in inbox} | {mail(sent)}
    assert deletes == []
    assert stats["mail_changed"] == 6
    assert set(state["mail"]["delta_links"]) == {"inbox", "sentitems"}


def test_every_request_asks_for_immutable_ids_text_bodies_and_sends_the_token(graph):
    graph.put_message("inbox", "Hello")

    sync({}, make_config(include_calendar=False))

    for request in graph.requests:
        assert 'IdType="ImmutableId"' in request.headers["Prefer"]
        assert 'outlook.body-content-type="text"' in request.headers["Prefer"]
        assert request.headers["Authorization"] == "Bearer test-token"
    assert "%24select=" in graph.requests[0].url and "body" in graph.requests[0].url


def test_mail_row_shape_and_content(graph):
    message_id = graph.put_message(
        "inbox",
        "Project Falcon budget",
        "The drone battery lasts 55 minutes.",
        ccRecipients=[{"emailAddress": {"name": "Arjun Rao", "address": "arjun@example.com"}}],
        hasAttachments=True,
    )

    upserts, _, _ = sync({}, make_config(include_calendar=False))

    assert upserts[mail(message_id)] == {
        "id": mail(message_id),
        "title": "Project Falcon budget",
        "content": (
            "Email: Project Falcon budget\n"
            "From: Meera Joshi <meera@example.com>\n"
            "To: Lay <lay@example.com>\n"
            "Cc: Arjun Rao <arjun@example.com>\n"
            "Sent: 2026-05-20T09:00:00Z\n"
            "Folder: Inbox\n"
            "Thread: AAQk-thread-1\n"
            "Attachments: yes, not ingested\n\n"
            "The drone battery lasts 55 minutes."
        ),
        "url": f"https://outlook.live.com/owa/?ItemID={message_id}",
        "_deleted": False,
    }


def test_headers_only_mode_does_not_request_bodies(graph):
    graph.put_message("inbox", "Hello", "secret body")

    upserts, _, _ = sync({}, make_config(include_calendar=False, include_mail_bodies=False))

    assert "body" not in graph.requests[0].url.split("%24select=")[1].split("&")[0].split("%2C")
    (row,) = upserts.values()
    assert "secret body" not in row["content"]


def test_received_after_filter_is_sent_on_the_first_round(graph):
    graph.put_message("inbox", "Old", receivedDateTime="2025-01-01T00:00:00Z")
    new = graph.put_message("inbox", "New", receivedDateTime="2026-05-01T00:00:00Z")

    upserts, _, _ = sync(
        {},
        make_config(
            include_calendar=False,
            mail_folders=("inbox",),
            mail_received_after="2026-01-01T00:00:00Z",
        ),
    )

    assert set(upserts) == {mail(new)}


# ---------------------------------------------------------------------------
# Mail: incremental rounds, edits, moves and deletes
# ---------------------------------------------------------------------------
def test_resync_without_changes_yields_nothing_but_moves_the_delta_link(graph):
    graph.put_message("inbox", "Hello")
    state: dict = {}
    sync(state, make_config(include_calendar=False))
    first_link = state["mail"]["delta_links"]["inbox"]
    graph.put_message("archive", "Unrelated")

    upserts, deletes, _ = sync(state, make_config(include_calendar=False))

    assert upserts == {} and deletes == []
    assert state["mail"]["delta_links"]["inbox"] == first_link  # nothing new in inbox


def test_edit_reyields_the_same_message(graph):
    message_id = graph.put_message("inbox", "Draft plan")
    state: dict = {}
    sync(state, make_config(include_calendar=False))

    graph.edit_message("inbox", message_id, subject="Final plan")
    upserts, deletes, _ = sync(state, make_config(include_calendar=False))

    assert list(upserts) == [mail(message_id)]
    assert upserts[mail(message_id)]["title"] == "Final plan"
    assert deletes == []


def test_move_between_synced_folders_is_an_update_not_a_delete(graph):
    message_id = graph.put_message("inbox", "Keep me")
    state: dict = {}
    config = make_config(include_calendar=False, mail_folders=("inbox", "archive"))
    sync(state, config)

    graph.move_message(message_id, "inbox", "archive")
    upserts, deletes, _ = sync(state, config)

    assert deletes == []
    assert "Folder: Archive" in upserts[mail(message_id)]["content"]
    assert state["mail"]["members"][message_id] == ["archive"]


def test_move_split_across_two_runs_is_still_not_a_delete(graph):
    """The message shows up in Archive before Inbox reports it removed."""
    message_id = graph.put_message("inbox", "Keep me")
    state: dict = {}
    config = make_config(include_calendar=False, mail_folders=("inbox", "archive"))
    sync(state, config)

    graph.move_message(message_id, "inbox", "archive")
    # Simulate a run that read Inbox before the move and Archive after it.
    inbox_removal = graph.mail_log["inbox"].pop()
    sync(state, config)
    graph.mail_log["inbox"].append(inbox_removal)
    _, deletes, _ = sync(state, config)

    assert deletes == []
    assert state["mail"]["members"][message_id] == ["archive"]


def test_deleting_mail_moves_it_to_deleted_items_and_forgets_it(graph):
    keep = graph.put_message("inbox", "Keep")
    gone = graph.put_message("inbox", "Delete me")
    state: dict = {}
    sync(state, make_config(include_calendar=False))

    graph.delete_message(gone, "inbox")
    upserts, deletes, stats = sync(state, make_config(include_calendar=False))

    assert upserts == {}
    assert deletes == [mail(gone)]
    assert stats["mail_deleted"] == 1
    assert set(state["mail"]["members"]) == {keep}


def test_move_to_an_unsynced_folder_forgets_the_message(graph):
    message_id = graph.put_message("inbox", "Moving out")
    state: dict = {}
    sync(state, make_config(include_calendar=False))

    graph.move_message(message_id, "inbox", "archive")
    _, deletes, _ = sync(state, make_config(include_calendar=False))

    assert deletes == [mail(message_id)]


def test_expired_delta_token_resyncs_the_folder_like_a_snapshot(graph):
    keep = graph.put_message("inbox", "Keep")
    gone = graph.put_message("inbox", "Gone")
    state: dict = {}
    sync(state, make_config(include_calendar=False))

    graph.delete_message(gone, "inbox")
    graph.expired_tokens.add(state["mail"]["delta_links"]["inbox"].split("$deltatoken=")[1])
    upserts, deletes, _ = sync(state, make_config(include_calendar=False))

    assert deletes == [mail(gone)]
    assert set(upserts) == {mail(keep)}  # a fresh round lists the folder again


def test_deleted_folder_is_confirmed_then_forgotten(graph):
    message_id = graph.put_message("archive", "In a folder that will vanish")
    state: dict = {}
    config = make_config(include_calendar=False, mail_folders=("archive",))
    sync(state, config)

    graph.delete_folder("archive")
    upserts, deletes, _ = sync(state, config)

    assert upserts == {}
    assert deletes == [mail(message_id)]
    assert state["mail"]["delta_links"] == {}


def test_dropping_a_folder_from_the_config_forgets_its_mail(graph):
    inbox = graph.put_message("inbox", "Inbox")
    sent = graph.put_message("sentitems", "Sent")
    state: dict = {}
    sync(state, make_config(include_calendar=False))

    _, deletes, _ = sync(state, make_config(include_calendar=False, mail_folders=("inbox",)))

    assert deletes == [mail(sent)]
    assert set(state["mail"]["members"]) == {inbox}


def test_a_bad_token_raises_and_never_deletes(graph):
    graph.put_message("inbox", "Keep")
    state: dict = {}
    sync(state, make_config(include_calendar=False))
    before = copy.deepcopy(state)

    graph.fail_next.append((401, {"error": {"code": "InvalidAuthenticationToken"}}, {}))
    with pytest.raises(OutlookError, match="HTTP 401"):
        sync(state, make_config(include_calendar=False))

    assert state == before


@pytest.mark.parametrize("status, code", [(403, "ErrorAccessDenied"), (400, "BadRequest")])
def test_one_failing_folder_never_deletes_and_does_not_stop_the_others(graph, status, code):
    kept = graph.put_message("inbox", "Keep")
    state: dict = {}
    sync(state, make_config(include_calendar=False))
    inbox_link = state["mail"]["delta_links"]["inbox"]
    graph.delete_message(kept, "inbox")
    new = graph.put_message("sentitems", "Arrives while Inbox fails")

    graph.fail_next.append((status, {"error": {"code": code}}, {}))
    upserts, deletes, stats = sync(state, make_config(include_calendar=False))

    assert deletes == [], "an error never deletes"
    assert set(upserts) == {mail(new)}, "Sent Items still syncs"
    assert state["mail"]["delta_links"]["inbox"] == inbox_link, "Inbox is read again next run"
    assert stats["mail_folders_failed"] == 1 and stats["failed"] == 1

    _, deletes, _ = sync(state, make_config(include_calendar=False))
    assert deletes == [mail(kept)]


def test_throttling_is_retried_after_retry_after(graph):
    message_id = graph.put_message("inbox", "Hello")
    graph.fail_next.append((429, {"error": {"code": "TooManyRequests"}}, {"Retry-After": "0"}))

    upserts, _, _ = sync({}, make_config(include_calendar=False, mail_folders=("inbox",)))

    assert set(upserts) == {mail(message_id)}


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------
CAL = {"include_mail": False}


def test_first_calendar_sync_reads_the_window_only(graph):
    inside = graph.put_event("Weekly sync", NOW + timedelta(days=2))
    graph.put_event("Far future", NOW + timedelta(days=90))
    state: dict = {}

    upserts, _, stats = sync(state, make_config(**CAL))

    assert set(upserts) == {event(inside)}
    assert stats["events_changed"] == 1
    assert state["calendar"]["known"] == [inside]


def test_event_row_shape_and_content(graph):
    event_id = graph.put_event(
        "Weekly sync",
        NOW + timedelta(days=2),
        seriesMasterId="AAMk-series-1",
        onlineMeeting={"joinUrl": "https://teams.example/join"},
        body={"contentType": "text", "content": "Agenda: battery tests."},
    )

    upserts, _, _ = sync({}, make_config(**CAL))

    assert upserts[event(event_id)]["content"] == (
        "Calendar event: Weekly sync\n"
        "When: 2026-06-03 12:00 to 13:00 (UTC)\n"
        "Location: Pune office\n"
        "Organizer: Meera Joshi <meera@example.com>\n"
        "Attendees: Lay <lay@example.com> (accepted)\n"
        "Series: AAMk-series-1\n"
        "Online meeting: https://teams.example/join\n\n"
        "Agenda: battery tests."
    )


def test_deleted_event_is_checked_by_id_then_forgotten(graph):
    keep = graph.put_event("Keep", NOW + timedelta(days=1))
    gone = graph.put_event("Gone", NOW + timedelta(days=3))
    state: dict = {}
    sync(state, make_config(**CAL))

    graph.delete_event(gone)
    upserts, deletes, stats = sync(state, make_config(**CAL))

    assert upserts == {}
    assert deletes == [event(gone)]
    assert stats["events_deleted"] == 1
    assert state["calendar"]["known"] == [keep]


def test_event_moved_out_of_the_window_is_kept_not_forgotten(graph):
    event_id = graph.put_event("Postponed", NOW + timedelta(days=3))
    state: dict = {}
    sync(state, make_config(**CAL))

    graph.edit_event(event_id, start=NOW + timedelta(days=120))
    _, deletes, stats = sync(state, make_config(**CAL))

    assert deletes == []
    assert stats["events_left_window"] == 1
    assert state["calendar"]["known"] == []


def test_recurring_series_is_one_document_and_bare_occurrences_are_skipped(graph):
    series = graph.put_series(
        "Falcon daily sync",
        NOW + timedelta(days=1),
        days=3,
        body={"contentType": "text", "content": "Navigation tests."},
    )
    state: dict = {}

    upserts, deletes, stats = sync(state, make_config(**CAL))

    assert list(upserts) == [event(series)]
    content = upserts[event(series)]["content"]
    assert "Repeats: daily from 2026-06-02 until 2026-06-04" in content
    assert "Navigation tests." in content
    assert stats["events_changed"] == 1
    assert state["calendar"]["known"] == [series]

    # A later round with no changes must not treat the occurrences as removed.
    upserts, deletes, _ = sync(state, make_config(**CAL))
    assert upserts == {} and deletes == []


def test_series_master_outside_the_window_is_fetched_by_id(graph):
    series = graph.put_series("Weekly sync since last year", NOW + timedelta(days=1), days=3)
    graph.hide_from_calendar_view(series)  # only the occurrences are listed
    state: dict = {}

    upserts, deletes, _ = sync(state, make_config(**CAL))

    assert list(upserts) == [event(series)]
    assert "Repeats: daily" in upserts[event(series)]["content"]
    assert deletes == []
    assert state["calendar"]["known"] == [series]

    # A window refresh must not mistake the unlisted master for a removal.
    upserts, deletes, stats = sync(state, make_config(**CAL), now=NOW + timedelta(days=8))
    assert deletes == []
    assert stats["events_left_window"] == 0
    assert state["calendar"]["known"] == [series]


@pytest.mark.parametrize("code", ["syncStateNotFound", "SyncStateNotFound", "resyncRequired"])
def test_expired_token_codes_are_matched_case_insensitively(graph, code):
    keep = graph.put_message("inbox", "Keep")
    state: dict = {}
    config = make_config(include_calendar=False, mail_folders=("inbox",))
    sync(state, config)

    graph.fail_next.append((400, {"error": {"code": code}}, {}))
    upserts, deletes, _ = sync(state, config)

    assert deletes == []
    assert set(upserts) == {mail(keep)}  # resynced instead of raising


def test_totals_use_the_keys_other_connectors_publish(graph):
    graph.put_message("inbox", "One")
    gone = graph.put_event("Gone", NOW + timedelta(days=2))
    state: dict = {}
    sync(state)
    graph.delete_event(gone)

    _, _, stats = sync(state)

    assert stats["deleted"] == 1
    assert stats["scanned"] == 0


def test_cancelled_meeting_is_forgotten(graph):
    event_id = graph.put_event("Cancelled sync", NOW + timedelta(days=3))
    state: dict = {}
    sync(state, make_config(**CAL))

    graph.edit_event(event_id, isCancelled=True)
    upserts, deletes, _ = sync(state, make_config(**CAL))

    assert upserts == {}
    assert deletes == [event(event_id)]


def test_window_refresh_reconciles_and_checks_missing_events(graph):
    soon = graph.put_event("Soon", NOW + timedelta(days=2))
    old = graph.put_event("Old", NOW - timedelta(days=25))
    deleted = graph.put_event("Deleted", NOW + timedelta(days=5))
    state: dict = {}
    sync(state, make_config(**CAL))
    first_link = state["calendar"]["delta_link"]

    graph.delete_event(deleted)
    later = NOW + timedelta(days=10)  # past the 7-day refresh: a fresh window
    upserts, deletes, stats = sync(state, make_config(**CAL), now=later)

    assert state["calendar"]["delta_link"] != first_link
    assert deletes == [event(deleted)]
    assert stats["events_left_window"] == 1  # "Old" fell out of the window
    assert set(upserts) == {event(soon)}
    assert state["calendar"]["known"] == [soon]
    assert old not in state["calendar"]["known"]


def test_expired_calendar_token_resyncs(graph):
    keep = graph.put_event("Keep", NOW + timedelta(days=2))
    state: dict = {}
    sync(state, make_config(**CAL))

    graph.expired_tokens.add(state["calendar"]["delta_link"].split("$deltatoken=")[1])
    upserts, deletes, _ = sync(state, make_config(**CAL))

    assert deletes == []
    assert set(upserts) == {event(keep)}


def test_event_lookup_error_keeps_the_event_and_checks_again_next_run(graph):
    gone = graph.put_event("Gone", NOW + timedelta(days=3))
    state: dict = {}
    sync(state, make_config(**CAL))

    graph.delete_event(gone)
    # The delta round succeeds; the by-id lookup that would confirm the delete fails.
    original = graph._calendar_delta

    def delta_then_fail_lookup(query):
        graph.fail_next.append((403, {"error": {"code": "ErrorAccessDenied"}}, {}))
        return original(query)

    graph._calendar_delta = delta_then_fail_lookup
    _, deletes, stats = sync(state, make_config(**CAL))

    assert deletes == [], "an unconfirmed removal never deletes"
    assert graph.requests[-1].url.endswith(f"/me/events/{gone}")
    assert stats["events_failed"] == 1 and stats["failed_calendar"] == 0
    assert state["calendar"]["recheck"] == {gone: 1}

    graph._calendar_delta = original
    _, deletes, _ = sync(state, make_config(**CAL))

    assert deletes == [event(gone)], "the next run confirms it with a 404"
    assert state["calendar"]["recheck"] == {}


def test_an_event_that_stays_unreadable_is_given_up_after_three_runs(graph):
    stuck = graph.put_event("Stuck", NOW + timedelta(days=1))
    state: dict = {}
    sync(state, make_config(**CAL))
    graph.delete_event(stuck)
    original = graph._handle

    def forbidden_for_stuck(request):
        if request.url.split("?")[0].endswith(f"/me/events/{stuck}"):
            graph.requests.append(request)
            return 403, {}, json.dumps({"error": {"code": "ErrorAccessDenied"}})
        return original(request)

    graph._handle = forbidden_for_stuck
    for run in (1, 2):
        new = graph.put_event(f"New meeting {run}", NOW + timedelta(days=2))
        upserts, deletes, stats = sync(state, make_config(**CAL))

        assert set(upserts) == {event(new)}, "new meetings keep arriving"
        assert deletes == [] and stats["events_failed"] == 1
        assert state["calendar"]["recheck"] == {stuck: run}

    # Unreadable is not gone: stop asking, but keep it, since errors never delete.
    _, deletes, stats = sync(state, make_config(**CAL))
    assert deletes == []
    assert stats["events_given_up"] == 1 and stats["events_failed"] == 0
    assert state["calendar"]["recheck"] == {} and stuck not in state["calendar"]["known"]

    sync(state, make_config(**CAL))
    lookups = [r for r in graph.requests if r.url.split("?")[0].endswith(f"/me/events/{stuck}")]
    assert len(lookups) == 3, "never looked up again"


def test_series_master_that_fails_to_load_is_fetched_next_run(graph, no_retry_wait):
    master = graph.put_series("Standup", NOW - timedelta(days=60), days=90)
    graph.hide_from_calendar_view(master)
    original = graph._handle
    broken = True

    def server_error_for_master(request):
        if broken and request.url.split("?")[0].endswith(f"/me/events/{master}"):
            graph.requests.append(request)
            return 500, {}, json.dumps({"error": {"code": "InternalServerError"}})
        return original(request)

    graph._handle = server_error_for_master
    state: dict = {}
    upserts, _, stats = sync(state, make_config(**CAL))

    assert event(master) not in upserts
    assert stats["events_failed"] == 1
    assert state["calendar"]["retry_masters"] == {master: 0}, "a 5xx is not the event's fault"

    broken = False
    upserts, _, _ = sync(state, make_config(**CAL))

    assert event(master) in upserts
    assert state["calendar"]["retry_masters"] == {}


# ---------------------------------------------------------------------------
# Mail and calendar together
# ---------------------------------------------------------------------------
def test_mail_and_events_share_one_table_with_prefixed_ids(graph):
    message_id = graph.put_message("inbox", "Agenda")
    event_id = graph.put_event("Weekly sync", NOW + timedelta(days=2))

    upserts, _, _ = sync({})

    assert set(upserts) == {mail(message_id), event(event_id)}


def test_attendee_order_from_graph_does_not_change_the_content(graph):
    ada = {"emailAddress": {"name": "Ada", "address": "ada@example.com"}, "status": {}}
    bob = {"emailAddress": {"name": "Bob", "address": "bob@example.com"}, "status": {}}
    first = graph.put_event("Sync", NOW + timedelta(days=1), attendees=[bob, ada])
    second = graph.put_event("Sync", NOW + timedelta(days=1), attendees=[ada, bob])

    upserts, _, _ = sync({}, make_config(**CAL))

    content = upserts[event(first)]["content"]
    assert content == upserts[event(second)]["content"]
    assert content.index("ada@example.com") < content.index("bob@example.com")


@pytest.mark.parametrize(
    ("days", "expected"),
    [(1, "When: all day, 2026-06-03"), (3, "When: all day, 2026-06-03 to 2026-06-05")],
)
def test_all_day_event_ends_on_its_last_day_not_the_exclusive_end(graph, days, expected):
    # Graph ends an all-day event at midnight after its last day.
    day = {"dateTime": "2026-06-03T00:00:00.0000000", "timeZone": "UTC"}
    end = {"dateTime": f"2026-06-{3 + days:02d}T00:00:00.0000000", "timeZone": "UTC"}
    holiday = graph.put_event("Holiday", NOW + timedelta(days=2), isAllDay=True)
    graph.events[holiday].update(start=day, end=end)

    upserts, _, _ = sync({}, make_config(**CAL))

    assert expected in upserts[event(holiday)]["content"].splitlines()


# ---------------------------------------------------------------------------
# Mail and calendar fail separately
# ---------------------------------------------------------------------------
def test_a_failing_mail_round_keeps_its_state_while_the_calendar_finishes(graph):
    graph.put_message("inbox", "Hello")
    meeting = graph.put_event("Weekly sync", NOW + timedelta(days=2))
    graph.fail_next.append((403, {"error": {"code": "ErrorAccessDenied"}}, {}))
    state: dict = {}

    upserts, deletes, stats = sync(state, make_config(mail_folders=("inbox",)))

    assert set(upserts) == {event(meeting)}
    assert deletes == []
    assert "mail" not in state and "calendar" in state
    assert stats["failed"] == 1 and stats["failed_mail"] == 1


def test_a_calendar_round_that_fails_leaves_nothing_behind_while_mail_finishes(graph):
    message_id = graph.put_message("inbox", "Hello")
    cancelled = graph.put_event("Cancelled later", NOW + timedelta(days=2))
    removed = graph.put_event("Deleted later", NOW + timedelta(days=3))
    state: dict = {}
    sync(state, make_config(mail_folders=("inbox",)))
    calendar_before = dict(state["calendar"])

    graph.edit_message("inbox", message_id, subject="Hello again")
    graph.edit_event(cancelled, isCancelled=True)
    graph.delete_event(removed)
    original = graph._calendar_delta

    def delta_then_fail_lookup(query):
        graph.fail_next.append((401, {"error": {"code": "InvalidAuthenticationToken"}}, {}))
        return original(query)

    graph._calendar_delta = delta_then_fail_lookup
    upserts, deletes, stats = sync(state, make_config(mail_folders=("inbox",)))

    # The cancelled meeting was already queued for forgetting when the lookup
    # failed; it must not leak out of the failed calendar round.
    assert deletes == []
    assert set(upserts) == {mail(message_id)}
    assert state["calendar"] == calendar_before
    assert stats["failed_auth"] == 1 and stats["events_deleted"] == 0


def test_mail_and_calendar_both_failing_raises_and_saves_nothing(graph):
    graph.put_message("inbox", "Hello")
    graph.put_event("Weekly sync", NOW + timedelta(days=2))
    for _ in range(2):
        graph.fail_next.append((403, {"error": {"code": "ErrorAccessDenied"}}, {}))
    state: dict = {}

    with pytest.raises(OutlookError, match="HTTP 403"):
        sync(state, make_config(mail_folders=("inbox",)))

    assert state == {}


@pytest.mark.parametrize(("status", "key"), [(401, "failed_auth"), (429, "failed_rate_limit")])
def test_auth_and_throttling_end_the_run(graph, status, key, no_retry_wait):
    graph.put_message("inbox", "Hello")
    graph.put_event("Weekly sync", NOW + timedelta(days=2))
    # More 429s than the client retries, so throttling outlasts the retries.
    for _ in range(10):
        graph.fail_next.append((status, {"error": {"code": "Nope"}}, {"Retry-After": "0"}))
    stats: dict = {}

    with pytest.raises(OutlookError, match=f"HTTP {status}"):
        sync({}, make_config(mail_folders=("inbox",)), stats=stats)

    assert stats[key] == 1
    assert not any("calendarView" in r.url for r in graph.requests), "calendar not tried"


def test_a_long_retry_after_is_capped_and_retried_only_a_few_times(graph, monkeypatch):
    graph.put_message("inbox", "Hello")
    waits: list[float] = []
    monkeypatch.setattr(outlook_module, "_sleep", waits.append)
    # Graph asks for a whole day; cognee holds its dlt staging lock meanwhile.
    for _ in range(10):
        graph.fail_next.append(
            (429, {"error": {"code": "TooManyRequests"}}, {"Retry-After": "86400"})
        )
    stats: dict = {}

    with pytest.raises(OutlookError, match="HTTP 429"):
        sync({}, make_config(include_calendar=False, mail_folders=("inbox",)), stats=stats)

    assert waits == [60, 60], "3 attempts, at most a minute apart"
    assert stats["failed_rate_limit"] == 1


def test_a_wait_is_slept_in_steps_and_cancelled_by_check_active(graph, monkeypatch):
    graph.put_message("inbox", "Hello")
    waits: list[float] = []
    monkeypatch.setattr(outlook_module, "_sleep", waits.append)
    graph.fail_next.append((429, {"error": {"code": "TooManyRequests"}}, {"Retry-After": "30"}))

    def check_active():
        if len(waits) == 3:
            raise PermissionError("revoked")

    client = build_graph_client(
        GraphAuth(GraphTokenProvider(access_token="test-token"), BASE), BASE, check_active
    )
    config = make_config(include_calendar=False, mail_folders=("inbox",))

    with pytest.raises(PermissionError):
        list(_iter_rows(client, config, {}, {}, now=NOW, check_active=check_active))

    assert waits == [1, 1, 1], "stopped 3 s into a 30 s wait"


def test_check_active_is_asked_before_a_retried_request(graph, monkeypatch):
    graph.put_message("inbox", "Hello")
    monkeypatch.setattr(outlook_module, "_sleep", lambda _seconds: None)
    graph.fail_next.append((503, {"error": {"code": "ServiceUnavailable"}}, {"Retry-After": "1"}))
    checks: list[int] = []
    client = build_graph_client(
        GraphAuth(GraphTokenProvider(access_token="test-token"), BASE),
        BASE,
        lambda: checks.append(len(graph.requests)),
    )
    config = make_config(include_calendar=False, mail_folders=("inbox",))

    rows = list(_iter_rows(client, config, {}, {}, now=NOW))

    assert [row["title"] for row in rows] == ["Hello"]
    assert checks == [1], "asked once, after the 503 and before the retry"


def test_a_repeated_page_link_fails_its_part_instead_of_looping(graph):
    graph.put_message("inbox", "One")
    graph.put_message("inbox", "Two")
    graph.put_message("inbox", "Three")  # page_size 2, so a second page exists
    original = graph._page

    def same_next_link_forever(items, final_link):
        status, headers, body = original(items, final_link)
        page = json.loads(body)
        page.pop("@odata.deltaLink", None)
        page["@odata.nextLink"] = f"{BASE}/next?$skiptoken=stuck"
        graph._pages["stuck"] = (items, final_link)
        return status, headers, json.dumps(page)

    def same_next_link_a_few_times(items, final_link):
        # Ends after 10 pages, so without the guard this fails instead of hanging.
        if len(graph.requests) > 10:
            return original(items, final_link)
        return same_next_link_forever(items, final_link)

    graph._page = same_next_link_a_few_times
    stats: dict = {}

    with pytest.raises(OutlookError, match="same page twice"):
        sync({}, make_config(include_calendar=False, mail_folders=("inbox",)), stats=stats)

    assert len(graph.requests) < 10


def test_a_lost_connection_is_a_clean_error_without_the_url(graph, no_retry_wait):
    graph.put_event("Weekly sync", NOW + timedelta(days=2))

    def unreachable(folder, query):
        raise requests.ConnectionError(f"Max retries exceeded with url: {BASE}/me/{folder}")

    graph._mail_delta = unreachable
    stats: dict = {}

    with pytest.raises(OutlookError, match="could not be reached") as error:
        sync({}, make_config(mail_folders=("inbox",)), stats=stats)

    assert "graph.test" not in str(error.value)
    assert stats["failed_connection"] == 1
    assert not any("calendarView" in r.url for r in graph.requests), "calendar not tried"


# ---------------------------------------------------------------------------
# Authorization checkpoint
# ---------------------------------------------------------------------------
class _RevokedError(Exception):
    pass


@pytest.mark.parametrize("allowed", [0, 1, 2, 3])
def test_check_active_runs_before_every_graph_request(graph, allowed):
    # Inbox spans two pages, then Sent Items: requests 1 and 2, then 3.
    # Zero: revoked before the run's very first request.
    for i in range(3):
        graph.put_message("inbox", f"In {i}")
    graph.put_message("sentitems", "Out")
    state: dict = {}

    def check_active():
        if len(graph.requests) >= allowed:
            raise _RevokedError

    with pytest.raises(_RevokedError):
        sync(state, make_config(include_calendar=False), check_active=check_active)

    assert len(graph.requests) == allowed, "no request after the revocation"
    assert state == {}


def test_check_active_guards_every_event_lookup(graph):
    first = graph.put_event("Gone 1", NOW + timedelta(days=3))
    second = graph.put_event("Gone 2", NOW + timedelta(days=4))
    state: dict = {}
    sync(state, make_config(**CAL))
    graph.delete_event(first)
    graph.delete_event(second)
    graph.requests.clear()

    def check_active():
        if any("/me/events/" in r.url for r in graph.requests):  # after the first lookup
            raise _RevokedError

    with pytest.raises(_RevokedError):
        sync(state, make_config(**CAL), check_active=check_active)

    assert sum("/me/events/" in r.url for r in graph.requests) == 1


def test_check_active_is_not_swallowed_by_the_per_part_error_handling(graph):
    graph.put_message("inbox", "Hello")
    graph.put_event("Weekly sync", NOW + timedelta(days=2))

    def check_active():
        if any("calendarView" in r.url for r in graph.requests) or (
            graph.requests and "messages/delta" in graph.requests[-1].url
        ):
            raise _RevokedError

    with pytest.raises(_RevokedError):
        sync({}, make_config(mail_folders=("inbox",)), check_active=check_active)


# ---------------------------------------------------------------------------
# No ids in logs or errors
# ---------------------------------------------------------------------------
def test_a_round_without_a_delta_link_fails_its_part_without_the_url(graph):
    graph.put_message("inbox", "Hello")
    meeting = graph.put_event("Weekly sync", NOW + timedelta(days=2))
    original = graph._mail_delta

    def no_delta_link(folder, query):
        status, headers, body = original(folder, query)
        page = json.loads(body)
        page.pop("@odata.deltaLink", None)
        return status, headers, json.dumps(page)

    graph._mail_delta = no_delta_link
    stats: dict = {}
    state: dict = {}

    upserts, _, _ = sync(state, make_config(mail_folders=("inbox",)), stats=stats)

    # Not a lost connection: only mail fails, and the calendar still finishes.
    # The mail rows already yielded still load, so mail keeps only their ids.
    assert event(meeting) in upserts
    assert stats["failed_mail"] == 1 and stats["failed_connection"] == 0
    assert all(folders == ["inbox"] for folders in state["mail"]["members"].values())
    assert not state["mail"]["delta_links"]

    # An incremental round requests the saved deltaLink, delta token and all.
    graph._mail_delta = original
    mail_only = make_config(include_calendar=False, mail_folders=("inbox",))
    mail_state: dict = {}
    sync(mail_state, mail_only)
    graph._mail_delta = no_delta_link
    with pytest.raises(OutlookError, match="without a deltaLink") as error:
        sync(mail_state, mail_only)
    assert "deltatoken" not in str(error.value) and "graph.test" not in str(error.value)


def test_no_ids_or_delta_tokens_reach_logs_or_error_text(graph, caplog):
    caplog.set_level(logging.DEBUG)
    graph.folders["AAMkSecretFolder"] = {}
    graph.mail_log["AAMkSecretFolder"] = []
    secret_message = graph.put_message("AAMkSecretFolder", "Secret plans")
    secret_event = graph.put_event("Secret meeting", NOW + timedelta(days=3), id="AAMk-secret-evt")
    config = make_config(mail_folders=("AAMkSecretFolder",))
    state: dict = {}
    sync(state, config)
    secret_token = state["mail"]["delta_links"]["AAMkSecretFolder"].split("$deltatoken=")[1]

    # Every path that logs or raises: an expired folder token, a vanished folder,
    # a failed event lookup, then a run where everything fails.
    graph.expired_tokens.add(secret_token)
    sync(state, config)
    graph.delete_event(secret_event)
    original = graph._calendar_delta

    def delta_then_fail_lookup(query):
        graph.fail_next.append((403, {"error": {"code": "ErrorAccessDenied"}}, {}))
        return original(query)

    graph._calendar_delta = delta_then_fail_lookup
    sync(state, config)
    graph.delete_folder("AAMkSecretFolder")
    sync(state, config)
    # Mail and the calendar round both fail, so nothing finishes and it raises.
    graph.fail_next.append((403, {"error": {"code": "ErrorAccessDenied"}}, {}))
    graph.fail_next.append((403, {"error": {"code": "ErrorAccessDenied"}}, {}))
    graph._calendar_delta = original
    with pytest.raises(OutlookError) as error:
        sync(state, config)

    assert caplog.text, "the paths above log"
    for secret in (
        "AAMkSecretFolder",
        secret_message,
        "AAMk-secret-evt",
        secret_token,
        "graph.test",
    ):
        assert secret not in caplog.text
        assert secret not in str(error.value)
    assert error.value.__cause__ is None, "the raw error, with its URL, is not chained"


# ---------------------------------------------------------------------------
# Review round: lookups, wait budget, mail folders, options and time zones
# ---------------------------------------------------------------------------
def event_lookups(graph):
    return [r for r in graph.requests if "/me/events/" in r.url]


def test_deleting_a_whole_series_forgets_only_what_was_stored(graph):
    series = graph.put_series("Daily standup", NOW + timedelta(days=1), days=20)
    state: dict = {}
    sync(state, make_config(**CAL))
    graph.requests.clear()

    graph.delete_series(series)
    _, deletes, stats = sync(state, make_config(**CAL))

    assert deletes == [event(series)], "the occurrences were never ingested"
    assert stats["events_deleted"] == 1 and stats["deleted"] == 1
    assert len(event_lookups(graph)) == 1, "only the master is looked up"


def test_a_meeting_cancelled_before_it_was_ever_synced_emits_no_tombstone(graph):
    state: dict = {}
    sync(state, make_config(**CAL))

    graph.put_event("Cancelled at once", NOW + timedelta(days=2), isCancelled=True)
    _, deletes, stats = sync(state, make_config(**CAL))

    assert deletes == [] and stats["events_deleted"] == 0


def test_lookups_stop_after_graph_fails_and_all_waits_share_one_budget(graph, monkeypatch):
    waits: list[float] = []
    monkeypatch.setattr(outlook_module, "_sleep", waits.append)
    gone = [graph.put_event(f"Gone {i}", NOW + timedelta(days=1)) for i in range(20)]
    state: dict = {}
    sync(state, make_config(**CAL))
    for event_id in gone:
        graph.delete_event(event_id)
    original = graph._handle

    def unavailable_lookups(request):
        if "/me/events/" in request.url:
            graph.requests.append(request)
            body = json.dumps({"error": {"code": "ServiceUnavailable"}})
            return 503, {"Retry-After": "60"}, body
        return original(request)

    graph._handle = unavailable_lookups
    graph.requests.clear()
    _, deletes, stats = sync(state, make_config(**CAL))

    assert waits == [60, 60], "two minutes for the whole run, not one per event"
    assert len(event_lookups(graph)) == 3, "one event, three attempts, then the rest wait"
    assert deletes == [] and stats["events_failed"] == 20
    assert set(state["calendar"]["recheck"]) == set(gone)

    graph._handle = original
    _, deletes, _ = sync(state, make_config(**CAL))
    assert deletes == sorted(event(i) for i in gone)


def test_an_event_lookup_that_is_not_json_fails_only_that_event(graph):
    keep = graph.put_event("Keep", NOW + timedelta(days=1))
    gone = graph.put_event("Gone", NOW + timedelta(days=2))
    state: dict = {}
    sync(state, make_config(**CAL))
    graph.delete_event(gone)
    new = graph.put_event("New", NOW + timedelta(days=3))
    original = graph._handle

    def proxy_page(request):
        if request.url.split("?")[0].endswith(f"/me/events/{gone}"):
            graph.requests.append(request)
            return 200, {"Content-Type": "text/html"}, "<html>Sign in to the proxy</html>"
        return original(request)

    graph._handle = proxy_page
    upserts, deletes, stats = sync(state, make_config(**CAL))

    assert set(upserts) == {event(new)} and deletes == []
    assert stats["events_failed"] == 1 and stats["failed_connection"] == 0
    assert state["calendar"]["recheck"] == {gone: 1}
    assert keep in state["calendar"]["known"]


def test_a_page_that_is_not_json_fails_its_folder_with_the_right_message(graph, caplog):
    graph.put_message("inbox", "Hello")
    sent = graph.put_message("sentitems", "Sent")
    original = graph._mail_delta

    def html_for_inbox(folder, query):
        if folder == "inbox":
            return 200, {"Content-Type": "text/html"}, "<html>proxy</html>"
        return original(folder, query)

    graph._mail_delta = html_for_inbox
    upserts, _, stats = sync({}, make_config(include_calendar=False))

    assert set(upserts) == {mail(sent)}
    assert stats["mail_folders_failed"] == 1 and stats["failed_connection"] == 0

    with pytest.raises(OutlookError, match="other than JSON"):
        sync({}, make_config(include_calendar=False, mail_folders=("inbox",)))


def test_a_known_series_master_is_fetched_again_when_its_occurrences_change(graph):
    series = graph.put_series("Standup", NOW + timedelta(days=1), days=3)
    graph.hide_from_calendar_view(series)  # delta never lists the master itself
    state: dict = {}
    upserts, _, _ = sync(state, make_config(**CAL))
    assert "Calendar event: Standup" in upserts[event(series)]["content"]

    graph.events[series]["subject"] = "Standup, new room"
    graph.event_log.append(("upsert", f"{series}-occ-1"))
    upserts, deletes, _ = sync(state, make_config(**CAL))

    assert "Calendar event: Standup, new room" in upserts[event(series)]["content"]
    assert deletes == []


def test_a_series_master_that_stays_unreadable_is_given_up(graph):
    master = graph.put_series("Standup", NOW - timedelta(days=60), days=90)
    graph.hide_from_calendar_view(master)
    original = graph._handle

    def forbidden_master(request):
        if request.url.split("?")[0].endswith(f"/me/events/{master}"):
            graph.requests.append(request)
            return 403, {}, json.dumps({"error": {"code": "ErrorAccessDenied"}})
        return original(request)

    graph._handle = forbidden_master
    state: dict = {}
    for tries in (1, 2):
        sync(state, make_config(**CAL))
        assert state["calendar"]["retry_masters"] == {master: tries}

    _, deletes, stats = sync(state, make_config(**CAL))
    assert stats["events_given_up"] == 1 and deletes == []
    assert state["calendar"]["retry_masters"] == {}


def test_older_state_with_list_shaped_rechecks_still_works(graph):
    gone = graph.put_event("Gone", NOW + timedelta(days=3))
    state: dict = {}
    sync(state, make_config(**CAL))
    graph.delete_event(gone)
    # The previous version already read this removal, failed the lookup and
    # saved the id in a plain list; delta will not report it again.
    link = state["calendar"]["delta_link"]
    position = link.split("$deltatoken=")[1].split("~")[1]
    state["calendar"]["delta_link"] = link.replace(
        f"cal~{position}~", f"cal~{len(graph.event_log)}~"
    )
    state["calendar"]["recheck"] = [gone]
    state["calendar"]["retry_masters"] = []

    _, deletes, _ = sync(state, make_config(**CAL))

    assert deletes == [event(gone)]
    assert state["calendar"]["recheck"] == {}


def test_turning_mail_bodies_on_later_reads_the_bodies(graph):
    message_id = graph.put_message("inbox", "Hello", "the body")
    state: dict = {}
    config = make_config(include_calendar=False, mail_folders=("inbox",))
    upserts, _, _ = sync(state, make_config(**{**config.__dict__, "include_mail_bodies": False}))
    assert "the body" not in upserts[mail(message_id)]["content"]

    later = graph.put_message("inbox", "Later", "later body")
    upserts, deletes, _ = sync(state, config)

    assert "the body" in upserts[mail(message_id)]["content"]
    assert "later body" in upserts[mail(later)]["content"]
    assert deletes == []


def test_removing_the_received_after_filter_reads_the_older_mail(graph):
    old = graph.put_message("inbox", "Old", receivedDateTime="2025-01-01T00:00:00Z")
    new = graph.put_message("inbox", "New", receivedDateTime="2026-05-01T00:00:00Z")
    state: dict = {}
    base = {"include_calendar": False, "mail_folders": ("inbox",)}
    upserts, _, _ = sync(state, make_config(**base, mail_received_after="2026-01-01T00:00:00Z"))
    assert set(upserts) == {mail(new)}

    upserts, deletes, _ = sync(state, make_config(**base))

    assert mail(old) in upserts and deletes == []


def test_a_stricter_received_after_forgets_the_older_mail(graph):
    old = graph.put_message("inbox", "Old", receivedDateTime="2025-01-01T00:00:00Z")
    graph.put_message("inbox", "New", receivedDateTime="2026-05-01T00:00:00Z")
    state: dict = {}
    base = {"include_calendar": False, "mail_folders": ("inbox",)}
    sync(state, make_config(**base))

    _, deletes, _ = sync(state, make_config(**base, mail_received_after="2026-01-01T00:00:00Z"))

    assert deletes == [mail(old)]


def test_mail_state_from_the_previous_version_gets_one_fresh_round(graph):
    keep = graph.put_message("inbox", "Keep")
    state: dict = {}
    config = make_config(include_calendar=False, mail_folders=("inbox",))
    sync(state, config)
    # The previous version saved neither the options nor pending moves.
    del state["mail"]["options"], state["mail"]["pending"]
    graph.requests.clear()

    upserts, deletes, _ = sync(state, config)

    assert set(upserts) == {mail(keep)} and deletes == []
    assert "%24deltatoken" not in graph.requests[0].url, "listed again with today's options"
    upserts, _, _ = sync(state, config)
    assert upserts == {}


def test_a_folder_name_that_was_never_synced_and_does_not_exist_raises(graph):
    graph.put_message("inbox", "Hello")
    state: dict = {}

    with pytest.raises(OutlookFolderNotFoundError, match='"nosuchfolder"'):
        sync(state, make_config(include_calendar=False, mail_folders=("inbox", "nosuchfolder")))

    assert state == {}


def test_a_folder_id_that_does_not_exist_is_named_by_position_not_by_id(graph):
    with pytest.raises(OutlookFolderNotFoundError, match="number 2") as error:
        sync({}, make_config(include_calendar=False, mail_folders=("inbox", "AAMkTypo123==")))

    assert "AAMkTypo123" not in str(error.value)


def test_mail_created_and_deleted_between_runs_is_not_counted(graph):
    state: dict = {}
    sync(state, make_config(include_calendar=False))

    message_id = graph.put_message("inbox", "Short-lived")
    graph.delete_message(message_id, "inbox")
    graph.mail_log["inbox"] = [e for e in graph.mail_log["inbox"] if e[0] == "removed"]
    _, deletes, stats = sync(state, make_config(include_calendar=False))

    assert deletes == [] and stats["mail_deleted"] == 0


def test_a_move_into_a_folder_read_earlier_is_not_a_delete(graph):
    """Inbox is read, then the message moves Archive -> Inbox, then Archive is read."""
    message_id = graph.put_message("archive", "Moving")
    state: dict = {}
    config = make_config(include_calendar=False, mail_folders=("inbox", "archive"))
    sync(state, config)

    graph.move_message(message_id, "archive", "inbox")
    inbox_arrival = graph.mail_log["inbox"].pop()  # Inbox was read before the move
    _, deletes, stats = sync(state, config)

    assert deletes == [] and stats["mail_deferred"] == 1
    assert state["mail"]["pending"] == {message_id: 1}

    graph.mail_log["inbox"].append(inbox_arrival)
    upserts, deletes, _ = sync(state, config)

    assert deletes == [] and "Folder: Inbox" in upserts[mail(message_id)]["content"]
    assert state["mail"]["pending"] == {}


def test_a_delete_reported_by_a_later_folder_is_forgotten_one_run_later(graph):
    message_id = graph.put_message("archive", "Delete me")
    state: dict = {}
    config = make_config(include_calendar=False, mail_folders=("inbox", "archive"))
    sync(state, config)

    graph.delete_message(message_id, "archive")
    _, deletes, _ = sync(state, config)
    assert deletes == []

    _, deletes, stats = sync(state, config)
    assert deletes == [mail(message_id)] and stats["mail_deleted"] == 1


def test_a_failed_folder_holds_back_deletes_until_it_is_read(graph):
    message_id = graph.put_message("inbox", "Moving to Sent")
    state: dict = {}
    config = make_config(include_calendar=False)
    sync(state, config)

    graph.move_message(message_id, "inbox", "sentitems")
    original = graph._mail_delta

    def sent_fails(folder, query):
        if folder == "sentitems":
            return 403, {}, json.dumps({"error": {"code": "ErrorAccessDenied"}})
        return original(folder, query)

    graph._mail_delta = sent_fails
    _, deletes, _ = sync(state, config)
    assert deletes == [], "Sent Items may hold it; nothing is decided while it fails"

    graph._mail_delta = original
    _, deletes, _ = sync(state, config)
    assert deletes == []
    assert state["mail"]["members"][message_id] == ["sentitems"]


def test_upserts_are_yielded_page_by_page(graph):
    for i in range(5):
        graph.put_message("inbox", f"Message {i}")  # page_size 2: three pages
    client = build_graph_client(GraphAuth(GraphTokenProvider(access_token="t"), BASE), BASE)
    rows = _iter_rows(client, make_config(include_calendar=False, mail_folders=("inbox",)), {})

    first = next(rows)

    assert not first["_deleted"]
    assert len(graph.requests) == 1, "the first row comes before the second page is asked for"
    assert len(list(rows)) == 4


def test_times_are_asked_for_in_utc_and_written_in_the_chosen_zone(graph):
    meeting = graph.put_event("Sync with Pune", NOW + timedelta(days=2))  # 12:00 UTC

    upserts, _, _ = sync({}, make_config(**CAL, time_zone="Asia/Kolkata"))

    assert 'outlook.timezone="UTC"' in graph.requests[0].headers["Prefer"]
    assert (
        "When: 2026-06-03 17:30 to 18:30 (Asia/Kolkata)"
        in upserts[event(meeting)]["content"].splitlines()
    )


@pytest.mark.parametrize(
    "zone", ["India Standard Time", "Asia/Kolkata", "tzone://Microsoft/Custom"]
)
def test_an_all_day_event_east_of_utc_keeps_its_own_day(graph, zone):
    # Midnight on 2026-11-08 in India is 18:30 UTC the day before.
    holiday = graph.put_event("Diwali", NOW + timedelta(days=2), isAllDay=True)
    graph.events[holiday].update(
        start={"dateTime": "2026-11-07T18:30:00.0000000", "timeZone": "UTC"},
        end={"dateTime": "2026-11-08T18:30:00.0000000", "timeZone": "UTC"},
        originalStartTimeZone=zone,
        originalEndTimeZone=zone,
    )

    upserts, _, _ = sync({}, make_config(**CAL))

    assert "When: all day, 2026-11-08" in upserts[event(holiday)]["content"].splitlines()


def test_an_all_day_event_west_of_utc_keeps_its_own_day(graph):
    # Midnight on 2026-11-08 in California is 08:00 UTC the same day.
    holiday = graph.put_event("Offsite", NOW + timedelta(days=2), isAllDay=True)
    graph.events[holiday].update(
        start={"dateTime": "2026-11-08T08:00:00.0000000", "timeZone": "UTC"},
        end={"dateTime": "2026-11-10T08:00:00.0000000", "timeZone": "UTC"},
        originalStartTimeZone="America/Los_Angeles",
        originalEndTimeZone="America/Los_Angeles",
    )

    upserts, _, _ = sync({}, make_config(**CAL))

    assert (
        "When: all day, 2026-11-08 to 2026-11-09" in upserts[event(holiday)]["content"].splitlines()
    )


def test_attendees_with_the_same_address_still_sort_the_same_way(graph):
    # A shared mailbox invited twice under two names: the address alone is a tie.
    shared = "team@example.com"
    one = {"emailAddress": {"name": "Ops", "address": shared}, "status": {"response": "accepted"}}
    two = {"emailAddress": {"name": "Room", "address": shared}, "status": {"response": "none"}}
    first = graph.put_event("Sync", NOW + timedelta(days=1), attendees=[one, two])
    second = graph.put_event("Sync", NOW + timedelta(days=1), attendees=[two, one])

    upserts, _, _ = sync({}, make_config(**CAL))

    assert upserts[event(first)]["content"] == upserts[event(second)]["content"]


def test_an_all_day_event_more_than_12_hours_east_uses_its_own_zone(graph):
    # Midnight on 2026-11-08 in Tonga (UTC+13) is 11:00 UTC the day before,
    # nearer to midnight on the 7th: only the event's own zone gets this right.
    holiday = graph.put_event("Holiday", NOW + timedelta(days=2), isAllDay=True)
    graph.events[holiday].update(
        start={"dateTime": "2026-11-07T11:00:00.0000000", "timeZone": "UTC"},
        end={"dateTime": "2026-11-08T11:00:00.0000000", "timeZone": "UTC"},
        originalStartTimeZone="Pacific/Tongatapu",
        originalEndTimeZone="Pacific/Tongatapu",
    )

    upserts, _, _ = sync({}, make_config(**CAL))

    assert "When: all day, 2026-11-08" in upserts[event(holiday)]["content"].splitlines()


def test_every_wait_of_a_run_comes_out_of_one_budget(graph, monkeypatch):
    waits: list[float] = []
    monkeypatch.setattr(outlook_module, "_sleep", waits.append)
    graph.put_message("inbox", "Hello")
    graph.put_message("sentitems", "Sent")
    graph.put_event("Weekly sync", NOW + timedelta(days=2))
    original = graph._handle
    throttled: set[str] = set()

    def busy_once_per_path(request):
        # Every request is answered 503 once, then served.
        path = request.url.split("?")[0]
        if path not in throttled:
            throttled.add(path)
            graph.requests.append(request)
            body = json.dumps({"error": {"code": "ServiceUnavailable"}})
            return 503, {"Retry-After": "60"}, body
        return original(request)

    graph._handle = busy_once_per_path
    upserts, _, stats = sync({}, make_config())

    assert waits == [60, 60], "Inbox and Sent Items used the two minutes"
    assert stats["failed_calendar"] == 1, "the calendar's 503 had no budget left"
    assert len(upserts) == 2


def test_a_folder_that_keeps_failing_holds_back_deletes_for_three_runs_only(graph):
    gone = graph.put_message("inbox", "Delete me")
    state: dict = {}
    config = make_config(include_calendar=False)
    sync(state, config)
    graph.delete_message(gone, "inbox")
    original = graph._mail_delta

    def sent_always_fails(folder, query):
        if folder == "sentitems":
            return 403, {}, json.dumps({"error": {"code": "ErrorAccessDenied"}})
        return original(folder, query)

    graph._mail_delta = sent_always_fails
    for run in (1, 2, 3):
        _, deletes, _ = sync(state, config)
        assert deletes == [] and state["mail"]["pending"] == {gone: run}

    _, deletes, _ = sync(state, config)
    assert deletes == [mail(gone)], "a broken folder never blocks deletes for good"
    assert state["mail"]["pending"] == {}


# ---------------------------------------------------------------------------
# Review round 6: rows yielded before a part fails, lookups, zones
# ---------------------------------------------------------------------------
def test_a_calendar_round_failing_on_page_two_still_forgets_a_loaded_event(graph, no_retry_wait):
    graph.put_message("inbox", "Mail is fine")
    for day in (1, 2, 3):
        graph.put_event(f"Meeting {day}", NOW + timedelta(days=day))
    state: dict = {}
    original = graph._handle

    def page_two_fails(request):
        if "$skiptoken" in request.url:
            graph.requests.append(request)
            return 503, {}, json.dumps({"error": {"code": "ServiceUnavailable"}})
        return original(request)

    graph._handle = page_two_fails
    upserts, _, stats = sync(state, make_config(mail_folders=("inbox",)))
    loaded = [i for i in upserts if i.startswith("event:")]
    assert len(loaded) == 2 and stats["failed_calendar"] == 1
    assert sorted(event(i) for i in state["calendar"]["known"]) == sorted(loaded)

    graph._handle = original
    gone = loaded[0].removeprefix("event:")
    graph.delete_event(gone)
    _, deletes, _ = sync(state, make_config(mail_folders=("inbox",)))

    assert deletes == [event(gone)], "the loaded row does not stay behind"


def test_a_single_mail_folder_failing_on_page_two_still_forgets_a_loaded_message(
    graph, no_retry_wait
):
    for subject in ("One", "Two", "Three"):
        graph.put_message("inbox", subject)
    graph.put_event("Calendar is fine", NOW + timedelta(days=1))  # one page
    state: dict = {}
    original = graph._handle

    def page_two_fails(request):
        if "$skiptoken" in request.url:
            graph.requests.append(request)
            return 503, {}, json.dumps({"error": {"code": "ServiceUnavailable"}})
        return original(request)

    graph._handle = page_two_fails
    upserts, _, stats = sync(state, make_config(mail_folders=("inbox",)))
    loaded = [i for i in upserts if i.startswith("mail:")]
    assert len(loaded) == 2 and stats["failed_mail"] == 1
    first = loaded[0].removeprefix("mail:")

    graph._handle = original
    graph.delete_message(first, "inbox")
    _, deletes, _ = sync(state, make_config(mail_folders=("inbox",)))

    assert mail(first) in deletes


def test_a_5xx_on_lookups_never_forgets_events_that_still_exist(graph, no_retry_wait):
    ids = [graph.put_event(f"Meeting {n}", NOW + timedelta(days=1)) for n in range(3)]
    state: dict = {}
    sync(state, make_config(**CAL))
    for event_id in ids:
        graph.hide_from_calendar_view(event_id)  # left the window, still there
    original = graph._handle

    def lookups_unavailable(request):
        if "/me/events/" in request.url:
            graph.requests.append(request)
            return 503, {}, json.dumps({"error": {"code": "ServiceUnavailable"}})
        return original(request)

    graph._handle = lookups_unavailable
    for run in range(1, 10):
        # A week on each time, so every run is a fresh window round.
        later = NOW + timedelta(days=8 * run)
        _, deletes, _ = sync(state, make_config(**CAL), now=later)
        assert deletes == []
    # Each took its turn going first; after 3 runs of 5xx in a row it is given
    # up, which keeps it, so the outage ends without forgetting anything.
    assert state["calendar"]["recheck"] == {} and state["calendar"]["outage"] == {}


def test_an_event_whose_lookup_meets_a_5xx_goes_last_next_run(graph, no_retry_wait):
    stuck, other = sorted(graph.put_event(f"M{n}", NOW + timedelta(days=1)) for n in range(2))
    state: dict = {}
    sync(state, make_config(**CAL))
    graph.delete_event(stuck)
    graph.delete_event(other)
    original = graph._handle

    def stuck_is_unavailable(request):
        if request.url.split("?")[0].endswith(f"/me/events/{stuck}"):
            graph.requests.append(request)
            return 500, {}, json.dumps({"error": {"code": "InternalServerError"}})
        return original(request)

    graph._handle = stuck_is_unavailable
    _, deletes, _ = sync(state, make_config(**CAL))
    assert deletes == [], "the 5xx on the first lookup skips the rest"
    _, deletes, _ = sync(state, make_config(**CAL))
    assert deletes == [event(other)], "the stuck one went last, so the other got its turn"


def test_a_master_that_was_never_stored_is_not_tombstoned(graph):
    master = graph.put_series("Standup", NOW - timedelta(days=60), days=90)
    graph.hide_from_calendar_view(master)
    original = graph._handle

    def master_gone(request):
        if request.url.split("?")[0].endswith(f"/me/events/{master}"):
            graph.requests.append(request)
            return 404, {}, json.dumps({"error": {"code": "ErrorItemNotFound"}})
        return original(request)

    graph._handle = master_gone
    _, deletes, _ = sync({}, make_config(**CAL))

    assert deletes == []


def test_a_429_keeps_the_lookup_attempts_counted_before_it(graph, no_retry_wait):
    first, second = sorted(graph.put_event(f"M{n}", NOW + timedelta(days=1)) for n in range(2))
    state: dict = {}
    sync(state, make_config(**CAL))
    graph.delete_event(first)
    graph.delete_event(second)
    original = graph._handle

    def first_forbidden_then_throttled(request):
        path = request.url.split("?")[0]
        if path.endswith(f"/me/events/{first}"):
            graph.requests.append(request)
            return 403, {}, json.dumps({"error": {"code": "ErrorAccessDenied"}})
        if path.endswith(f"/me/events/{second}"):
            graph.requests.append(request)
            return 429, {}, json.dumps({"error": {"code": "TooManyRequests"}})
        return original(request)

    graph._handle = first_forbidden_then_throttled
    with pytest.raises(OutlookError):
        sync(state, make_config(**CAL))
    # The run raised, so dlt saves nothing; one that finishes mail keeps the count.
    _, _, stats = sync(state, make_config(mail_folders=("inbox",)))
    assert stats["failed_rate_limit"] == 1
    assert state["calendar"]["recheck"][first] == 1


@pytest.mark.parametrize(
    "zone, start, day",
    [
        ("Tonga Standard Time", "2026-11-07T11:00:00", "2026-11-08"),  # UTC+13
        ("Line Islands Standard Time", "2026-11-07T10:00:00", "2026-11-08"),  # UTC+14
        ("Dateline Standard Time", "2026-11-08T12:00:00", "2026-11-08"),  # UTC-12
        ("New Zealand Standard Time", "2026-11-07T11:00:00", "2026-11-08"),  # summer, +13
    ],
)
def test_an_all_day_event_uses_its_windows_zone_name(graph, zone, start, day):
    holiday = graph.put_event("Holiday", NOW + timedelta(days=2), isAllDay=True)
    graph.events[holiday].update(
        start={"dateTime": f"{start}.0000000", "timeZone": "UTC"},
        end={
            "dateTime": f"{start}.0000000".replace(start[:10], _next_day(start)),
            "timeZone": "UTC",
        },
        originalStartTimeZone=zone,
        originalEndTimeZone=zone,
    )

    upserts, _, _ = sync({}, make_config(**CAL))

    assert f"When: all day, {day}" in upserts[event(holiday)]["content"].splitlines()


def _next_day(start: str) -> str:
    from datetime import date

    return (date.fromisoformat(start[:10]) + timedelta(days=1)).isoformat()


def test_changing_time_zone_rewrites_the_events_in_the_new_zone(graph):
    meeting = graph.put_event("Planning", NOW + timedelta(days=1))
    state: dict = {}
    sync(state, make_config(**CAL))

    upserts, _, _ = sync(state, make_config(**CAL, time_zone="Asia/Kolkata"))

    assert "(Asia/Kolkata)" in upserts[event(meeting)]["content"]
    assert state["calendar"]["time_zone"] == "Asia/Kolkata"


# ---------------------------------------------------------------------------
# Review round 7: a 5xx that never ends, and what a failed part keeps
# ---------------------------------------------------------------------------
def answer(graph, statuses):
    """Answer the lookup of each id in ``statuses`` with that status."""
    original = graph._handle

    def handle(request):
        event_id = request.url.split("?")[0].rsplit("/me/events/", 1)[-1]
        if "/me/events/" in request.url and event_id in statuses:
            graph.requests.append(request)
            return statuses[event_id], {}, json.dumps({"error": {"code": "Error"}})
        return original(request)

    graph._handle = handle


def test_a_master_answering_500_does_not_hold_up_a_removal(graph, no_retry_wait):
    master = graph.put_series("Standup", NOW - timedelta(days=60), days=90)
    graph.hide_from_calendar_view(master)
    gone = graph.put_event("Review", NOW + timedelta(days=1))
    state: dict = {}
    sync(state, make_config(**CAL))

    graph.delete_event(gone)
    answer(graph, {master: 500})
    _, deletes, _ = sync(state, make_config(**CAL))
    assert deletes == [event(gone)], "the removal has its own lookups"

    for _ in range(3):
        sync(state, make_config(**CAL))
    assert state["calendar"]["retry_masters"] == {}, "a master answering 500 is given up"
    assert state["calendar"]["outage"] == {}


def test_two_events_answering_500_do_not_starve_a_third(graph, no_retry_wait):
    a1, a2, z = (graph.put_event(n, NOW + timedelta(days=1), id=n) for n in ("a1", "a2", "z"))
    state: dict = {}
    sync(state, make_config(**CAL))
    for event_id in (a1, a2, z):
        graph.delete_event(event_id)
    answer(graph, {a1: 500, a2: 500})

    forgotten = []
    for _ in range(3):
        _, deletes, _ = sync(state, make_config(**CAL))
        forgotten += deletes

    assert forgotten == [event(z)]


def test_runs_of_5xx_are_kept_when_the_calendar_part_fails(graph, no_retry_wait):
    graph.put_message("inbox", "Mail is fine")
    slow, throttled = (graph.put_event(n, NOW + timedelta(days=1), id=n) for n in ("a", "b"))
    state: dict = {}
    sync(state, make_config(mail_folders=("inbox",)))
    graph.delete_event(slow)
    graph.delete_event(throttled)
    master = graph.put_series("Standup", NOW - timedelta(days=60), days=90)
    graph.hide_from_calendar_view(master)
    answer(graph, {master: 500, throttled: 429})

    # The master meets a 500, then the removal of a 404 and b 429 end the part.
    _, _, stats = sync(state, make_config(mail_folders=("inbox",)))

    assert stats["failed_rate_limit"] == 1
    assert state["calendar"]["outage"] == {master: 1}


def test_a_master_yielded_before_the_part_failed_is_still_forgotten(graph, no_retry_wait):
    graph.put_message("inbox", "Mail is fine")
    stuck = graph.put_event("Stuck", NOW + timedelta(days=1), id="stuck")
    state: dict = {}
    sync(state, make_config(mail_folders=("inbox",)))
    graph.delete_event(stuck)
    master = graph.put_series("Standup", NOW - timedelta(days=60), days=90)
    graph.hide_from_calendar_view(master)
    answer(graph, {stuck: 429})

    upserts, _, _ = sync(state, make_config(mail_folders=("inbox",)))
    assert event(master) in upserts
    assert master in state["calendar"]["known"]

    graph._handle = FakeGraph._handle.__get__(graph)
    graph.delete_series(master)
    _, deletes, _ = sync(state, make_config(mail_folders=("inbox",)))
    assert event(master) in deletes


def test_a_folder_that_yielded_rows_before_failing_is_listed_fresh_next_run(graph, no_retry_wait):
    graph.put_message("inbox", "Before")
    graph.put_event("Calendar is fine", NOW + timedelta(days=1))
    state: dict = {}
    sync(state, make_config(mail_folders=("inbox", "sentitems")))
    assert "inbox" in state["mail"]["delta_links"]
    for subject in ("One", "Two", "Three"):
        graph.put_message("inbox", subject)
    original = graph._handle

    def inbox_page_two_fails(request):
        if "$skiptoken" in request.url:
            graph.requests.append(request)
            return 503, {}, json.dumps({"error": {"code": "ServiceUnavailable"}})
        return original(request)

    graph._handle = inbox_page_two_fails
    _, _, stats = sync(state, make_config(mail_folders=("inbox", "sentitems")))

    assert stats["mail_folders_failed"] == 1
    assert "inbox" not in state["mail"]["delta_links"], "an old link may not replay it"
    assert "sentitems" in state["mail"]["delta_links"]


def test_a_mail_part_that_fails_after_yielding_drops_that_folders_link(graph, no_retry_wait):
    graph.put_message("inbox", "Before")
    graph.put_event("Calendar is fine", NOW + timedelta(days=1))
    state: dict = {}
    sync(state, make_config(mail_folders=("inbox",)))
    for subject in ("One", "Two", "Three"):
        graph.put_message("inbox", subject)
    original = graph._handle

    def page_two_fails(request):
        if "$skiptoken" in request.url:
            graph.requests.append(request)
            return 503, {}, json.dumps({"error": {"code": "ServiceUnavailable"}})
        return original(request)

    graph._handle = page_two_fails
    _, _, stats = sync(state, make_config(mail_folders=("inbox",)))

    assert stats["failed_mail"] == 1
    assert "inbox" not in state["mail"]["delta_links"]
