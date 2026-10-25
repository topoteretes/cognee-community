"""Unit and dlt-pipeline tests for the Zoom connector. No live Zoom account needed.

* Client tests run ``ZoomClient`` against ``httpx.MockTransport``: token grant,
  reuse and renewal, retries, error codes, and no token leak on redirects.
* Parser and rendering tests cover transcripts (VTT), chat layouts and rows.
* Sync tests drive ``_iter_rows`` with a fake API and a plain dict as state, and
  run ``zoom_source`` through a dlt pipeline into a temp sqlite destination to
  prove the incremental cursor and forget-on-delete.
"""

import base64
import copy
from datetime import date, timedelta
from itertools import pairwise
from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

import httpx
import pytest
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item
from conftest import NOW, FakeZoom

from cognee_community_connector_zoom import zoom as zoom_module
from cognee_community_connector_zoom.zoom import (
    ZoomAPIError,
    ZoomAuthError,
    ZoomClient,
    ZoomRateLimitedError,
    _date_slices,
    _iter_rows,
    _paginate,
    _ZoomConfig,
    parse_chat,
    parse_vtt,
    render_meeting,
    zoom_source,
)

VTT = """WEBVTT

1
00:00:01.000 --> 00:00:04.000
Priya Shah: We need the export fix before Friday.

2
00:00:04.500 --> 00:00:06.000
Priya Shah: It blocks the release.

3
00:00:06.500 --> 00:00:09.000
Sam Lee: I'll take it, the PR is almost ready.
"""

CHAT = "00:02:10\tSam Lee:\thttps://example.com/pull/42\n"


def _config(**overrides):
    values = {
        "user_ids": (),
        "since": None,
        "include_transcripts": True,
        "include_chat": True,
        "transcript_wait_hours": 24.0,
    }
    values.update(overrides)
    return _ZoomConfig(**values)


def _run(zoom, state, now=NOW, stats=None, **overrides):
    stats = stats if stats is not None else {}
    return list(_iter_rows(zoom, _config(**overrides), state, stats, now=lambda: now))


TOMORROW = NOW + timedelta(days=1)


# ---------------------------------------------------------------------------
# ZoomClient over httpx.MockTransport
# ---------------------------------------------------------------------------
class _Api:
    """Scripted Zoom endpoints. ``routes`` maps (method, host + path) to responses."""

    def __init__(self, routes):
        self.routes = {key: list(value) for key, value in routes.items()}
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        queue = self.routes[(request.method, request.url.host + request.url.path)]
        return queue.pop(0) if len(queue) > 1 else queue[0]


def _token(token="tok-1", expires_in=3599):
    return httpx.Response(
        200,
        json={"access_token": token, "expires_in": expires_in, "api_url": "https://api.zoom.us"},
    )


def _client(api, clock=lambda: 0.0, **kwargs):
    sleeps = []
    client = ZoomClient(
        "acc",
        "cid",
        "secret",
        http=httpx.Client(transport=httpx.MockTransport(api), follow_redirects=True),
        sleep=sleeps.append,
        clock=clock,
        **kwargs,
    )
    return client, sleeps


def test_token_grant_uses_basic_auth_and_account_credentials():
    api = _Api(
        {
            ("POST", "zoom.us/oauth/token"): [_token()],
            ("GET", "api.zoom.us/v2/users"): [httpx.Response(200, json={"users": []})],
        }
    )
    client, _ = _client(api)
    client.get("/users", {"status": "active"})

    grant, call = api.requests
    expected = "Basic " + base64.b64encode(b"cid:secret").decode()
    assert grant.headers["Authorization"] == expected
    assert b"grant_type=account_credentials" in grant.content
    assert b"account_id=acc" in grant.content
    assert call.headers["Authorization"] == "Bearer tok-1"
    assert call.url.params["status"] == "active"


def test_token_is_reused_then_renewed_before_expiry():
    now = [0.0]
    api = _Api(
        {
            ("POST", "zoom.us/oauth/token"): [_token("tok-1"), _token("tok-2")],
            ("GET", "api.zoom.us/v2/users"): [httpx.Response(200, json={"users": []})],
        }
    )
    client, _ = _client(api, clock=lambda: now[0])
    client.get("/users")
    client.get("/users")
    now[0] = 3600.0
    client.get("/users")

    grants = [r for r in api.requests if r.method == "POST"]
    assert len(grants) == 2
    assert api.requests[-1].headers["Authorization"] == "Bearer tok-2"


def test_401_renews_the_token_once():
    api = _Api(
        {
            ("POST", "zoom.us/oauth/token"): [_token("tok-1"), _token("tok-2")],
            ("GET", "api.zoom.us/v2/users"): [
                httpx.Response(401, json={"code": 124}),
                httpx.Response(200, json={"users": []}),
            ],
        }
    )
    client, _ = _client(api)
    assert client.get("/users") == {"users": []}
    assert api.requests[-1].headers["Authorization"] == "Bearer tok-2"


def test_rejected_credentials_raise_auth_error_without_secrets():
    api = _Api({("POST", "zoom.us/oauth/token"): [httpx.Response(400, json={"reason": "x"})]})
    client, _ = _client(api)
    with pytest.raises(ZoomAuthError) as info:
        client.get("/users")
    assert "secret" not in str(info.value)
    assert "secret" not in repr(client)


def test_429_and_5xx_are_retried_honoring_retry_after():
    api = _Api(
        {
            ("POST", "zoom.us/oauth/token"): [_token()],
            ("GET", "api.zoom.us/v2/users"): [
                httpx.Response(429, headers={"Retry-After": "3"}),
                httpx.Response(503),
                httpx.Response(200, json={"users": []}),
            ],
        }
    )
    client, sleeps = _client(api)
    assert client.get("/users") == {"users": []}
    assert sleeps == [3.0, 2.0]


@pytest.mark.parametrize("retry_after", ["3600", "1"])
def test_long_or_endless_429_raises_rate_limited(retry_after):
    api = _Api(
        {
            ("POST", "zoom.us/oauth/token"): [_token()],
            ("GET", "api.zoom.us/v2/users"): [
                httpx.Response(429, headers={"Retry-After": retry_after})
            ],
        }
    )
    # A budget far above the wait, so only the long Retry-After stops it.
    client, sleeps = _client(api, max_sleep=10_000.0)
    with pytest.raises(ZoomRateLimitedError):
        client.get("/users")
    assert sleeps == ([] if retry_after == "3600" else [1.0] * 4)


def test_retry_waits_stop_at_the_budget_until_reset():
    api = _Api(
        {
            ("POST", "zoom.us/oauth/token"): [_token()],
            ("GET", "api.zoom.us/v2/users"): [
                httpx.Response(503, headers={"Retry-After": "40"}),
                httpx.Response(200, json={"users": []}),
            ],
        }
    )
    client, sleeps = _client(api, max_sleep=60.0)
    client.get("/users")
    api.routes[("GET", "api.zoom.us/v2/users")] = [
        httpx.Response(503, headers={"Retry-After": "40"}),
        httpx.Response(200, json={"users": []}),
    ]
    with pytest.raises(ZoomRateLimitedError):
        client.get("/users")
    client.reset_sleep_budget()
    assert client.get("/users") == {"users": []}
    assert sleeps == [40.0]


def test_error_carries_status_and_zoom_code_only():
    api = _Api(
        {
            ("POST", "zoom.us/oauth/token"): [_token()],
            ("GET", "api.zoom.us/v2/users/bob@example.com"): [
                httpx.Response(404, json={"code": 1001, "message": "User bob@example.com"}),
            ],
        }
    )
    client, _ = _client(api)
    with pytest.raises(ZoomAPIError) as info:
        client.get("/users/bob@example.com")
    assert (info.value.status, info.value.code) == (404, 1001)
    assert "bob" not in str(info.value)


def test_download_does_not_send_the_token_to_another_host(monkeypatch):
    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(api), **kwargs),
    )
    api = _Api(
        {
            ("POST", "zoom.us/oauth/token"): [_token()],
            ("GET", "zoom.us/rec/download/abc"): [
                httpx.Response(302, headers={"Location": "https://storage.test/file.vtt"})
            ],
            ("GET", "storage.test/file.vtt"): [httpx.Response(200, content=b"\xef\xbb\xbfWEBVTT")],
        }
    )
    # The client builds its own httpx.Client, so the shipped redirect handling is used.
    client = ZoomClient("acc", "cid", "secret")
    assert client.download("https://zoom.us/rec/download/abc") == "WEBVTT"
    first, redirected = api.requests[1], api.requests[2]
    assert first.headers["Authorization"] == "Bearer tok-1"
    assert "Authorization" not in redirected.headers


def test_download_refuses_a_url_off_zoom():
    api = _Api({("POST", "zoom.us/oauth/token"): [_token()]})
    client, _ = _client(api)
    with pytest.raises(ZoomAPIError, match="not on a Zoom host"):
        client.download("https://evil.test/zoom.us/rec/download/abc")
    assert api.requests == []


def test_client_requires_all_credentials():
    with pytest.raises(ValueError):
        ZoomClient("acc", "", "secret")


# ---------------------------------------------------------------------------
# Pagination and windows
# ---------------------------------------------------------------------------
class _Pages:
    def __init__(self, pages):
        self.pages = pages
        self.tokens = []

    def get(self, path, params=None):
        self.tokens.append((params or {}).get("next_page_token"))
        return self.pages[len(self.tokens) - 1]


def test_paginate_follows_next_page_token():
    pages = _Pages(
        [
            {"meetings": [{"uuid": "a"}], "next_page_token": "t1"},
            {"meetings": [{"uuid": "b"}], "next_page_token": ""},
        ]
    )
    assert [m["uuid"] for m in _paginate(pages, "/x", {}, "meetings")] == ["a", "b"]
    assert pages.tokens == [None, "t1"]


def test_paginate_refuses_an_answer_without_the_list():
    with pytest.raises(ZoomAPIError, match="without a meetings list"):
        list(_paginate(_Pages([{"next_page_token": ""}]), "/x", {}, "meetings"))
    empty = _Pages([{"total_records": 0, "next_page_token": ""}])
    assert list(_paginate(empty, "/x", {}, "meetings")) == []


def test_paginate_raises_when_the_token_does_not_advance():
    pages = _Pages([{"meetings": [], "next_page_token": "t1"}] * 2)
    with pytest.raises(ZoomAPIError):
        list(_paginate(pages, "/x", {}, "meetings"))


def test_date_slices_stay_within_one_month():
    slices = list(_date_slices(date(2026, 1, 1), date(2026, 3, 15)))
    assert slices[0] == (date(2026, 1, 1), date(2026, 1, 30))
    assert slices[-1][1] == date(2026, 3, 15)
    assert all((stop - start).days < 30 for start, stop in slices)
    assert all(b[0] == a[1] + timedelta(days=1) for a, b in pairwise(slices))


# ---------------------------------------------------------------------------
# Parsing and rendering
# ---------------------------------------------------------------------------
def test_parse_vtt_drops_timing_and_merges_turns():
    assert parse_vtt(VTT) == [
        "Priya Shah: We need the export fix before Friday. It blocks the release.",
        "Sam Lee: I'll take it, the PR is almost ready.",
    ]


def test_parse_vtt_keeps_lines_without_speaker():
    text = "WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.000\nHello there\n"
    assert parse_vtt(text) == ["Hello there"]


def test_parse_chat_handles_the_known_layouts():
    text = (
        "00:01:02\tPriya Shah:\tFirst message\n"
        "00:01:30\t From  Sam Lee : Second message\n"
        "00:02:00 From Ana Ruiz to Everyone:\n"
        "\tThird message\n"
        "\tstill third\n"
    )
    assert parse_chat(text) == [
        "Priya Shah: First message",
        "Sam Lee: Second message",
        "Ana Ruiz: Third message still third",
    ]


def test_parse_chat_leaves_direct_messages_out_unless_asked():
    text = (
        "00:01:02 From Sam Lee to Everyone:\n"
        "\tShipping Friday\n"
        "00:01:30 From Sam Lee to Priya Shah(Direct Message):\n"
        "\tsecret plan\n"
        "\tstill secret\n"
        "00:02:00\t From  Ana Ruiz to  Sam Lee(Privately) : psst\n"
        "00:02:10\tPriya Shah:\tSounds good\n"
    )
    assert parse_chat(text) == ["Sam Lee: Shipping Friday", "Priya Shah: Sounds good"]
    assert "Sam Lee to Priya Shah(Direct Message): secret plan still secret" in parse_chat(
        text, include_direct_messages=True
    )


def test_render_meeting_is_deterministic_and_flat():
    zoom = FakeZoom()
    meeting = zoom.add_meeting("uuid-1", transcript=VTT, chat=CHAT)
    row = render_meeting(meeting, ["Priya Shah: hi"], ["Sam Lee: link"], "Priya Shah")
    assert row == render_meeting(meeting, ["Priya Shah: hi"], ["Sam Lee: link"], "Priya Shah")
    assert set(row) == {"id", "title", "content", "url", "_deleted"}
    assert row["id"] == "meeting:uuid-1"
    assert row["title"] == "Weekly planning (2026-10-05)"
    assert row["content"].splitlines()[:4] == [
        "Meeting: Weekly planning",
        "Date: 2026-10-05 10:00 UTC",
        "Duration: 30 min",
        "Host: Priya Shah",
    ]
    assert "Transcript:\nPriya Shah: hi" in row["content"]
    assert "Chat:\nSam Lee: link" in row["content"]


def test_row_becomes_a_zoom_document():
    zoom = FakeZoom()
    row = render_meeting(zoom.add_meeting("uuid-1"), ["Priya Shah: hi"], [])
    dlt_row = SimpleNamespace(
        table_name="zoom_meetings", primary_key_value=row["id"], row_data=row, content_hash="h"
    )
    item = _build_document_data_item(dlt_row, uuid5(NAMESPACE_OID, row["id"]), "zoom")
    assert item.data.startswith("# Weekly planning (2026-10-05)")
    assert item.system_metadata["source"] == "zoom"
    assert item.system_metadata["external_id"] == "meeting:uuid-1"


# ---------------------------------------------------------------------------
# Sync state machine (_iter_rows with a dict as state)
# ---------------------------------------------------------------------------
def test_first_sync_emits_every_meeting_and_stores_the_window(zoom):
    zoom.add_meeting("m1", transcript=VTT, chat=CHAT)
    zoom.add_meeting("m2", topic="Retro", start="2026-10-06T15:00:00Z", transcript=VTT)
    state = {}
    rows = _run(zoom, state)

    assert sorted(r["id"] for r in rows) == ["meeting:m1", "meeting:m2"]
    m1 = next(r for r in rows if r["id"] == "meeting:m1")
    assert "Sam Lee: I'll take it, the PR is almost ready." in m1["content"]
    assert "Sam Lee: https://example.com/pull/42" in m1["content"]
    assert state["since"] == (NOW.date() - timedelta(days=30)).isoformat()
    assert set(state["meetings"]) == {"m1", "m2"}


def test_unchanged_meetings_are_not_downloaded_again(zoom):
    zoom.add_meeting("m1", transcript=VTT)
    state = {}
    _run(zoom, state)
    zoom.downloads.clear()

    assert _run(zoom, state) == []
    assert zoom.downloads == []


def test_only_new_meetings_are_downloaded(zoom):
    zoom.add_meeting("m1", transcript=VTT)
    state = {}
    _run(zoom, state)
    zoom.downloads.clear()
    zoom.add_meeting("m2", start="2026-10-07T09:00:00Z", transcript=VTT)

    rows = _run(zoom, state)
    assert [r["id"] for r in rows] == ["meeting:m2"]
    assert all("/m2/" in url for url in zoom.downloads)


def test_recurring_meeting_instances_stay_separate(zoom):
    zoom.add_meeting("instance-a", start="2026-10-01T10:00:00Z", transcript=VTT)
    zoom.add_meeting("instance-b", start="2026-10-08T10:00:00Z", transcript=VTT)
    rows = _run(zoom, {})
    assert sorted(r["id"] for r in rows) == ["meeting:instance-a", "meeting:instance-b"]


def test_recent_recording_without_transcript_waits_then_arrives(zoom):
    zoom.add_meeting("m1", start="2026-10-08T11:00:00Z", duration=20)
    state = {}
    assert _run(zoom, state) == []
    assert "m1" not in state["meetings"]

    zoom.add_meeting("m1", start="2026-10-08T11:00:00Z", duration=20, transcript=VTT)
    rows = _run(zoom, state)
    assert [r["id"] for r in rows] == ["meeting:m1"]
    assert "Transcript:" in rows[0]["content"]


def test_processing_files_are_not_ingested_yet(zoom):
    zoom.add_meeting("m1", start="2026-10-08T11:00:00Z", transcript=VTT, status="processing")
    assert _run(zoom, {}) == []


def test_transcript_that_never_comes_is_ingested_after_the_wait(zoom):
    zoom.add_meeting("m1", start="2026-10-08T11:00:00Z", duration=20, chat=CHAT)
    state = {}
    assert _run(zoom, state) == []

    rows = _run(zoom, state, now=NOW + timedelta(hours=25))
    assert [r["id"] for r in rows] == ["meeting:m1"]
    assert "Chat:" in rows[0]["content"]
    assert "Transcript:" not in rows[0]["content"]


def test_late_transcript_updates_an_ingested_meeting(zoom):
    zoom.add_meeting("m1", chat=CHAT)
    state = {}
    _run(zoom, state)

    zoom.add_meeting("m1", chat=CHAT, transcript=VTT)
    rows = _run(zoom, state)
    assert [r["id"] for r in rows] == ["meeting:m1"]
    assert "Transcript:" in rows[0]["content"]


def test_deleted_meeting_is_tombstoned_by_the_next_full_listing(zoom):
    zoom.add_meeting("m1", transcript=VTT)
    zoom.add_meeting("m2", transcript=VTT)
    state = {}
    _run(zoom, state)

    del zoom.meetings["m2"]
    assert _run(zoom, state, now=NOW + timedelta(hours=1)) == []
    assert _run(zoom, state, now=TOMORROW) == [{"id": "meeting:m2", "_deleted": True}]
    assert "m2" not in state["meetings"]


def test_runs_between_full_listings_list_only_the_last_month(zoom):
    zoom.add_meeting("m1", transcript=VTT)
    state = {}
    _run(zoom, state, since=date(2026, 6, 1))
    full = [p for path, p in zoom.calls if path.endswith("/recordings")]
    assert full[0]["from"] == "2026-06-01" and len(full) == 5
    assert all(p["page_size"] == 300 for path, p in zoom.calls if path.startswith("/users"))

    zoom.calls.clear()
    _run(zoom, state, now=NOW + timedelta(hours=1))
    recent = [p for path, p in zoom.calls if path.endswith("/recordings")]
    assert [(p["from"], p["to"]) for p in recent] == [("2026-09-09", "2026-10-08")]


def test_listing_that_comes_back_empty_waits_for_confirmation(zoom):
    zoom.add_meeting("m1", transcript=VTT)
    state = {}
    _run(zoom, state)

    zoom.meetings.clear()
    assert _run(zoom, state, now=TOMORROW) == []
    assert state["meetings"] == {"m1": state["meetings"]["m1"]}
    assert _run(zoom, state, now=TOMORROW) == [{"id": "meeting:m1", "_deleted": True}]
    assert "empty_listing" not in state


def test_answers_without_users_or_meetings_raise_instead_of_forgetting(zoom):
    zoom.add_meeting("m1", transcript=VTT)
    state = {}
    _run(zoom, state)

    zoom.drop_meetings_key = True
    with pytest.raises(ZoomAPIError, match="meetings list"):
        _run(zoom, state, now=TOMORROW)
    zoom.users = {"active": [], "inactive": []}
    with pytest.raises(ZoomAPIError, match="no users"):
        _run(zoom, state, now=TOMORROW)
    assert set(state["meetings"]) == {"m1"}


def test_files_that_never_finish_are_bounded_by_the_wait(zoom):
    zoom.add_meeting(
        "m1",
        start="2026-10-08T11:00:00Z",
        transcript=VTT,
        status="processing",
        file_status="processing",
    )
    assert _run(zoom, {}) == []

    stats = {}
    rows = _run(zoom, {}, now=NOW + timedelta(days=10), stats=stats)
    assert [r["id"] for r in rows] == ["meeting:m1"]
    assert "Transcript:" not in rows[0]["content"]
    assert stats["skipped_files"] == 1 and zoom.downloads == []


def test_gone_file_is_skipped_and_the_meeting_still_ingested(zoom):
    zoom.add_meeting("m1", transcript=VTT, chat=CHAT)
    zoom.gone_files.add("https://us02web.zoom.us/rec/download/m1/transcript")
    stats = {}
    rows = _run(zoom, {}, stats=stats)
    assert "Chat:" in rows[0]["content"] and "Transcript:" not in rows[0]["content"]
    assert stats["skipped_files"] == 1


def test_file_off_zoom_is_never_downloaded(zoom):
    meeting = zoom.add_meeting("m1", transcript=VTT)
    meeting["recording_files"][1]["download_url"] = "https://evil.test/rec/m1"
    stats = {}
    rows = _run(zoom, {}, stats=stats)
    assert [r["id"] for r in rows] == ["meeting:m1"]
    assert zoom.downloads == [] and stats["skipped_files"] == 1


def test_each_run_starts_a_new_wait_budget(zoom):
    resets = []
    zoom.reset_sleep_budget = lambda: resets.append(1)
    _run(zoom, {})
    _run(zoom, {})
    assert resets == [1, 1]


def test_regenerated_transcript_is_downloaded_again(zoom):
    zoom.add_meeting("m1", transcript=VTT)
    state = {}
    _run(zoom, state)

    zoom.add_meeting("m1", transcript=VTT.replace("Friday", "Monday"), version="-2")
    rows = _run(zoom, state)
    assert "Monday" in rows[0]["content"]


def test_direct_messages_stay_out_of_the_document_by_default(zoom):
    chat = "00:01:02\tSam Lee:\tpublic\n00:01:30 From Sam Lee to Priya Shah(Direct Message): dm\n"
    zoom.add_meeting("m1", chat=chat)
    state = {}
    rows = _run(zoom, state)
    assert "public" in rows[0]["content"] and "dm" not in rows[0]["content"]

    rows = _run(zoom, state, include_direct_messages=True)
    assert "dm" in rows[0]["content"]


def test_rate_limit_or_lost_token_after_progress_stops_cleanly(zoom):
    zoom.add_meeting("m1", transcript=VTT)
    zoom.add_meeting("m2", start="2026-10-06T10:00:00Z", transcript=VTT)
    state = {}
    _run(zoom, state)
    del zoom.meetings["m2"]
    zoom.add_meeting("m3", start="2026-10-07T10:00:00Z", transcript=VTT)
    zoom.add_meeting("m4", start="2026-10-07T11:00:00Z", transcript=VTT)
    real_download = zoom.download

    for error, reason in ((ZoomRateLimitedError, "rate_limit"), (ZoomAuthError, "auth")):
        run_state = copy.deepcopy(state)

        def download(url, error=error):
            if "/m4/" in url:
                raise error("stop")
            return real_download(url)

        zoom.download = download
        stats = {}
        rows = _run(zoom, run_state, now=TOMORROW, stats=stats)
        assert [r["id"] for r in rows] == ["meeting:m3"]
        assert stats["failed"] == 1 and stats[f"failed_{reason}"] == 1
        assert "m2" in run_state["meetings"] and "m3" in run_state["meetings"]


def test_lost_token_before_any_progress_raises(zoom):
    zoom.add_meeting("m1", transcript=VTT)

    def download(url):
        raise ZoomAuthError("Zoom rejected the access token (HTTP 401)")

    zoom.download = download
    with pytest.raises(ZoomAuthError):
        _run(zoom, {})


def test_deactivated_users_are_still_listed(zoom):
    zoom.users["inactive"] = [{"id": "u2", "first_name": "Ana", "last_name": "Ruiz"}]
    zoom.add_meeting("m1", host="u2", transcript=VTT)
    rows = _run(zoom, {})
    assert [r["id"] for r in rows] == ["meeting:m1"]
    assert "Host: Ana Ruiz" in rows[0]["content"]


def test_listing_failure_raises_before_any_row(zoom):
    zoom.add_meeting("m1", transcript=VTT)
    state = {}
    _run(zoom, state)
    zoom.fail_path = "/recordings"

    rows = _iter_rows(zoom, _config(), state, {}, now=lambda: NOW)
    with pytest.raises(ZoomAPIError):
        next(rows)
    assert set(state["meetings"]) == {"m1"}


def test_selected_users_are_looked_up_and_limit_the_sync(zoom):
    zoom.users["active"].append({"id": "u2", "display_name": "Sam Lee"})
    zoom.add_meeting("m1", host="u1", transcript=VTT)
    zoom.add_meeting("m2", host="u2", transcript=VTT)
    rows = _run(zoom, {}, user_ids=("u2",))
    assert [r["id"] for r in rows] == ["meeting:m2"]
    assert ("/users", {"status": "active"}) not in zoom.calls


def test_unknown_selected_user_raises(zoom):
    with pytest.raises(ZoomAPIError):
        _run(zoom, {}, user_ids=("nobody",))


def test_selected_users_without_the_lookup_scope_name_it(zoom):
    zoom.missing_user_scope = True
    with pytest.raises(ZoomAPIError, match="user:read:user:admin") as caught:
        _run(zoom, {}, user_ids=("u1",))
    assert caught.value.code == 4711


def test_stored_window_is_reused_and_moving_it_forgets_older_meetings(zoom):
    zoom.add_meeting("old", start="2026-09-20T10:00:00Z", transcript=VTT)
    zoom.add_meeting("new", start="2026-10-05T10:00:00Z", transcript=VTT)
    state = {}
    _run(zoom, state, since=date(2026, 9, 1))
    assert state["since"] == "2026-09-01"

    assert _run(zoom, state) == []
    assert _run(zoom, state, since=date(2026, 10, 1)) == [{"id": "meeting:old", "_deleted": True}]


def test_text_files_can_be_turned_off(zoom):
    zoom.add_meeting("m1", transcript=VTT, chat=CHAT)
    rows = _run(zoom, {}, include_transcripts=False, include_chat=False)
    assert zoom.downloads == []
    assert "Transcript:" not in rows[0]["content"]
    assert "Chat:" not in rows[0]["content"]


# ---------------------------------------------------------------------------
# zoom_source factory
# ---------------------------------------------------------------------------
def test_source_declares_document_path_and_scope(zoom):
    source = zoom_source(service=zoom, resource_name="zoom_team_a")
    assert getattr(source, dlt_utils.DOCUMENT_SOURCE_ATTR) == "zoom"
    assert getattr(source, dlt_utils.PIPELINE_SCOPE_ATTR) == "zoom_team_a"
    assert source.name == "zoom_team_a"
    assert zoom_source(client=zoom).name == "zoom_meetings"


def test_default_resource_name_comes_from_the_account(zoom):
    assert zoom_source("acme1", service=zoom).name == "zoom_acme1"
    upper, lower = zoom_source("AbC-1", service=zoom).name, zoom_source("abc_1", service=zoom).name
    assert upper.startswith("zoom_abc_1_") and lower == "zoom_abc_1"


def test_credentials_are_passed_by_name(monkeypatch):
    seen = {}
    monkeypatch.setattr(zoom_module, "ZoomClient", lambda **kwargs: seen.update(kwargs) or "c")
    zoom_source("acc", "cid", "secret")
    assert seen == {"account_id": "acc", "client_id": "cid", "client_secret": "secret"}


def test_missing_credentials_raise(monkeypatch):
    monkeypatch.setenv("ZOOM_CLIENT_SECRET", "from-env")
    with pytest.raises(ValueError, match="client_secret"):
        zoom_source(account_id="acc", client_id="cid")


# ---------------------------------------------------------------------------
# dlt pipeline: incremental cursor + forget-on-delete
# ---------------------------------------------------------------------------
@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def _pipeline(dlt, tmp_path):
    db_path = (tmp_path / "zoom.db").as_posix()
    return dlt.pipeline(
        pipeline_name="zoom_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="zoom_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def _staged(pipeline):
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, content FROM zoom_meetings") as cursor,
    ):
        return {row[0]: row[1] for row in cursor.fetchall()}


def test_pipeline_resyncs_only_changes_and_forgets_deletions(dlt_mod, tmp_path, zoom, clock):
    zoom.add_meeting("m1", transcript=VTT)
    zoom.add_meeting("m2", topic="Retro", transcript=VTT)
    pipeline = _pipeline(dlt_mod, tmp_path)

    pipeline.run(zoom_source(client=zoom))
    assert set(_staged(pipeline)) == {"meeting:m1", "meeting:m2"}

    # The cursor lives in dlt state, so a fresh source object resumes from it.
    zoom.downloads.clear()
    source = zoom_source(client=zoom)
    pipeline.run(source)
    assert zoom.downloads == []
    assert source.cognee_sync_stats["unchanged"] == 2

    del zoom.meetings["m2"]
    zoom.add_meeting("m3", start="2026-10-07T09:00:00Z", transcript=VTT)
    clock[0] = TOMORROW
    pipeline.run(zoom_source(client=zoom))
    assert set(_staged(pipeline)) == {"meeting:m1", "meeting:m3"}


def test_two_sources_in_one_dataset_leave_each_other_alone(dlt_mod, tmp_path, clock):
    team_a, team_b = FakeZoom(), FakeZoom()
    team_a.add_meeting("a1", transcript=VTT)
    team_a.add_meeting("a2", transcript=VTT)
    team_b.add_meeting("b1", transcript=VTT)
    pipeline = _pipeline(dlt_mod, tmp_path)
    pipeline.run(zoom_source(service=team_a, resource_name="zoom_a"))
    pipeline.run(zoom_source(service=team_b, resource_name="zoom_b"))

    del team_a.meetings["a1"]
    clock[0] = TOMORROW
    pipeline.run(zoom_source(service=team_a, resource_name="zoom_a"))
    pipeline.run(zoom_source(service=team_b, resource_name="zoom_b"))
    with pipeline.sql_client() as client:
        with client.execute_query("SELECT id FROM zoom_a") as cursor:
            assert [row[0] for row in cursor.fetchall()] == ["meeting:a2"]
        with client.execute_query("SELECT id FROM zoom_b") as cursor:
            assert [row[0] for row in cursor.fetchall()] == ["meeting:b1"]


def test_failed_run_keeps_staging_and_cursor(dlt_mod, tmp_path, zoom, clock):
    zoom.add_meeting("m1", transcript=VTT)
    zoom.add_meeting("m2", topic="Retro", transcript=VTT)
    pipeline = _pipeline(dlt_mod, tmp_path)
    pipeline.run(zoom_source(client=zoom))

    del zoom.meetings["m1"]
    zoom.fail_path = "/recordings"
    clock[0] = TOMORROW
    with pytest.raises(Exception, match="503"):
        pipeline.run(zoom_source(client=zoom))
    assert set(_staged(pipeline)) == {"meeting:m1", "meeting:m2"}

    zoom.fail_path = None
    pipeline.run(zoom_source(client=zoom))
    assert set(_staged(pipeline)) == {"meeting:m2"}
