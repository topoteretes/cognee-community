"""Sync state machine tests: _iter_rows against an in-memory Microsoft Graph.

No network, no LLM. The fake Graph is served over HTTP with ``responses``, so
dlt's real RESTClient, the nextLink paginator and its retries all run.
"""

from datetime import timedelta

import pytest
import responses
from fake_graph import BASE, NOW, FakeGraph
from requests import HTTPError

from cognee_community_connector_outlook.graph_auth import GraphAuth, GraphTokenProvider
from cognee_community_connector_outlook.outlook import (
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
    }
    values.update(overrides)
    return _OutlookConfig(**values)


def sync(state, config=None, now=NOW):
    client = build_graph_client(GraphAuth(GraphTokenProvider(access_token="test-token")), BASE)
    stats: dict[str, int] = {}
    rows = list(_iter_rows(client, config or make_config(), state, stats, now=now))
    upserts = {row["id"]: row for row in rows if not row["_deleted"]}
    deletes = sorted(row["id"] for row in rows if row["_deleted"])
    return upserts, deletes, stats


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


@pytest.mark.parametrize(
    "status, code",
    [(401, "InvalidAuthenticationToken"), (403, "ErrorAccessDenied"), (400, "BadRequest")],
)
def test_errors_raise_and_never_delete(graph, status, code):
    graph.put_message("inbox", "Keep")
    state: dict = {}
    sync(state, make_config(include_calendar=False))
    before = {"mail": {k: dict(v) for k, v in state["mail"].items()}}

    graph.fail_next.append((status, {"error": {"code": code}}, {}))
    with pytest.raises(HTTPError):
        sync(state, make_config(include_calendar=False))

    assert state == before


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
        "When: 2026-06-03T12:00:00.0000000 UTC to 2026-06-03T13:00:00.0000000 UTC\n"
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


def test_event_lookup_error_raises_instead_of_deleting(graph):
    gone = graph.put_event("Gone", NOW + timedelta(days=3))
    state: dict = {}
    sync(state, make_config(**CAL))
    before = {"calendar": dict(state["calendar"])}

    graph.delete_event(gone)
    # The delta round succeeds; the by-id lookup that would confirm the delete fails.
    original = graph._calendar_delta

    def delta_then_fail_lookup(query):
        graph.fail_next.append((403, {"error": {"code": "ErrorAccessDenied"}}, {}))
        return original(query)

    graph._calendar_delta = delta_then_fail_lookup
    with pytest.raises(HTTPError):
        sync(state, make_config(**CAL))

    assert state == before
    assert graph.requests[-1].url.endswith(f"/me/events/{gone}")


# ---------------------------------------------------------------------------
# Mail and calendar together
# ---------------------------------------------------------------------------
def test_mail_and_events_share_one_table_with_prefixed_ids(graph):
    message_id = graph.put_message("inbox", "Agenda")
    event_id = graph.put_event("Weekly sync", NOW + timedelta(days=2))

    upserts, _, _ = sync({})

    assert set(upserts) == {mail(message_id), event(event_id)}
