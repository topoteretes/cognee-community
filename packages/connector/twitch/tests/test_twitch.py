"""Unit tests for the Twitch connector.

Helix is mocked by ``FakeTwitch`` (see fake_twitch.py; no token, no network),
so these run in CI. Coverage:

  - rendering: channel/video/clip rows, duration parsing, deterministic rows,
    node sets, ``view_count`` kept out of the content
  - a full sync: follows, channel info and descriptions, VODs and clips,
    with pagination followed through the ``after`` cursor
  - unfollow -> the channel and all of its stored content become ``_deleted``
  - expired VOD -> ``_deleted``; new VOD ingested; a renamed channel keeps
    its key and updates its row
  - ``channels=`` selection and the ``max_channels`` bound
  - a 429 with a short wait is retried; a long wait stops the run cleanly and
    the next run replays; 401 -> TwitchAuthError with no token anywhere
  - rate-limit headers: low remaining backs off to the reset, and a reset
    beyond a minute stops the run before the request
  - dlt wiring: merge + id PK + hard_delete + node-set column, the document
    path, the stats, and ``check_active``
"""

import io

import pytest
from fake_twitch import (
    CLIP_PARSER,
    FOLLOW_RUST,
    INFO_RUST,
    USER,
    VIDEO_RUST_ARCHIVE,
    VIDEO_RUST_HIGHLIGHT,
    VIDEO_RUST_NEW,
    VIDEO_RUST_UPLOAD,
    FakeTwitch,
    Headers,
    Opener,
    Response,
    http_error,
    in_seconds,
)

from cognee_community_connector_twitch import twitch as module
from cognee_community_connector_twitch.twitch import (
    TwitchAPIError,
    TwitchAuthError,
    TwitchClient,
    TwitchRateLimitError,
    channel_to_row,
    clip_to_row,
    deleted_row,
    parse_duration,
    sync_channels,
    twitch_source,
    video_to_row,
)

TOKEN = "secret-access-token-value"
USER_DICT = {"id": USER["id"], "login": USER["login"]}


def run(fake, state, **kwargs):
    stats = kwargs.pop("stats", None) or {}
    return list(sync_channels(fake, state, user=USER_DICT, stats=stats, **kwargs)), stats


def ids(rows):
    return sorted(row["id"] for row in rows)


FIRST_RUN_IDS = [
    "channel:2001",
    "channel:2002",
    "channel:2003",
    "clip:RandomClip1",
    "clip:RandomClip2",
    "video:1234567",
    "video:1234568",
    "video:1234569",
]


# --------------------------------------------------------------------- rendering
def test_channel_row_has_document_columns_and_node_set():
    row = channel_to_row(FOLLOW_RUST, INFO_RUST, "Streams about Rust.", 3, 0)
    assert row["id"] == "channel:2001"
    assert row["title"] == "RustStreams"
    assert "Twitch channel RustStreams (ruststreams)." in row["content"]
    assert "Description: Streams about Rust." in row["content"]
    assert "Current category: Science & Technology" in row["content"]
    assert "Followed: 2025-05-24 22:22 UTC" in row["content"]
    assert "Ingested: 3 VOD(s), 0 clip(s)." in row["content"]
    assert row["url"] == "https://www.twitch.tv/ruststreams"
    assert row["cognee_node_set"] == ["channel:ruststreams"]
    assert row["_deleted"] is False


def test_channel_row_renders_without_info_or_description():
    row = channel_to_row(FOLLOW_RUST, None, None, 0, 0)
    assert "Description" not in row["content"]
    assert "Current category" not in row["content"]
    assert "Followed" in row["content"]


def test_channel_row_without_an_id_or_login_is_skipped():
    assert channel_to_row({"broadcaster_id": "2001"}, None, "", 0, 0) is None
    assert channel_to_row({"broadcaster_login": "x"}, None, "", 0, 0) is None


def test_video_row_renders_duration_type_and_dates():
    row = video_to_row(VIDEO_RUST_ARCHIVE)
    assert row["id"] == "video:1234567"
    assert row["title"] == "Building a compiler, part 3"
    assert "Type: past broadcast" in row["content"]
    assert "Description: Type checking and monomorphization." in row["content"]
    assert "Published: 2025-09-01 20:05 UTC" in row["content"]
    assert "Duration: 2h 14m 33s" in row["content"]
    assert row["url"] == "https://www.twitch.tv/videos/1234567"
    assert row["cognee_node_set"] == ["channel:ruststreams"]


def test_highlight_and_upload_types_are_named():
    assert "Type: highlight reel" in video_to_row(VIDEO_RUST_HIGHLIGHT)["content"]
    upload = {**VIDEO_RUST_UPLOAD, "type": "upload"}
    assert "Type: upload" in video_to_row(upload)["content"]


def test_view_count_is_excluded_from_video_content():
    content = video_to_row(VIDEO_RUST_ARCHIVE)["content"]
    assert "1863062" not in content
    assert "view" not in content.lower()


def test_clip_row_renders_channel_creator_and_url():
    row = clip_to_row(CLIP_PARSER, "codecorner")
    assert row["id"] == "clip:RandomClip1"
    assert row["title"] == "The parser finally works"
    assert "from channel CodeCorner (codecorner)" in row["content"]
    assert "Created by: ClipFan" in row["content"]
    assert "Created: 2025-08-20 22:34 UTC" in row["content"]
    assert row["url"] == "https://clips.twitch.tv/RandomClip1"
    assert row["cognee_node_set"] == ["channel:codecorner"]
    assert "view" not in row["content"].lower()


@pytest.mark.parametrize(
    ("duration", "expected"),
    [
        ("PT2H14M33S", "2h 14m 33s"),
        ("2h14m33s", "2h 14m 33s"),
        ("3m21s", "3m 21s"),
        ("45s", "45s"),
        ("1h", "1h"),
        ("PT1H30M", "1h 30m"),
        ("PT0S", "0s"),
        ("not a duration", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_duration_parsing(duration, expected):
    assert parse_duration(duration) == expected


def test_rows_are_deterministic_and_survive_view_count_churn():
    first = video_to_row(VIDEO_RUST_ARCHIVE)
    assert first == video_to_row(dict(VIDEO_RUST_ARCHIVE))
    # view_count changes on every view; the row must not (its content hash
    # stays, so the VOD is not re-cognified on every run).
    assert first == video_to_row({**VIDEO_RUST_ARCHIVE, "view_count": 9999999})
    assert channel_to_row(FOLLOW_RUST, INFO_RUST, "d", 1, 2) == channel_to_row(
        dict(FOLLOW_RUST), dict(INFO_RUST), "d", 1, 2
    )
    assert clip_to_row(CLIP_PARSER, "codecorner") == clip_to_row(dict(CLIP_PARSER), "codecorner")


def test_deleted_row_is_a_bare_hard_delete_marker():
    assert deleted_row("video:1234567") == {"id": "video:1234567", "_deleted": True}


# --------------------------------------------------------------------- sync
def test_first_sync_lists_everything_with_pagination_followed():
    fake = FakeTwitch()
    state = {}
    rows, stats = run(fake, state)
    assert ids(rows) == FIRST_RUN_IDS
    assert stats["items"] == 8 and stats["deletions"] == 0
    assert stats["skipped"] == 0 and stats["failed"] == 0
    # follows span two pages (3 follows, 2 per page) via the after cursor
    follows_calls = fake.calls_to("channels/followed")
    assert len(follows_calls) == 2 and follows_calls[1]["after"] == "2"
    assert follows_calls[0]["user_id"] == USER["id"] and follows_calls[0]["first"] == 100
    # videos span two pages (3 VODs, 2 per page); channels without VODs make
    # one empty-page call each
    videos_calls = [params for params in fake.calls_to("videos") if params["user_id"] == "2001"]
    assert len(videos_calls) == 2
    assert videos_calls[0]["user_id"] == "2001" and videos_calls[0]["type"] == "all"
    assert videos_calls[1]["after"] == "2"
    # channel info and descriptions are batched over repeated params
    assert fake.calls_to("channels") == [{"broadcaster_id": ["2002", "2001", "2003"]}]
    assert fake.calls_to("users") == [{"id": ["2002", "2001", "2003"]}]
    # state written after the last row
    assert state["user_id"] == USER["id"]
    assert state["channels"] == {"2001": "ruststreams", "2002": "codecorner", "2003": "barechannel"}
    assert sorted(state["items"]["2001"]) == ["video:1234567", "video:1234568", "video:1234569"]
    assert sorted(state["items"]["2002"]) == ["clip:RandomClip1", "clip:RandomClip2"]
    assert state["items"]["2003"] == []


def test_unfollow_forgets_the_channel_and_all_its_stored_content():
    fake = FakeTwitch()
    state = {}
    run(fake, state)
    fake.unfollow("2001")
    rows, stats = run(fake, state)
    # the current listing is re-emitted whole; the unfollowed channel and all
    # of its stored content carry the hard-delete marker
    gone = {"channel:2001", "video:1234567", "video:1234568", "video:1234569"}
    by_id = {row["id"]: row for row in rows}
    assert set(by_id) == gone | {
        "channel:2002",
        "channel:2003",
        "clip:RandomClip1",
        "clip:RandomClip2",
    }
    assert all(by_id[key] == deleted_row(key) for key in gone)
    assert all(not row["_deleted"] for row in rows if row["id"] not in gone)
    assert stats["deletions"] == 4
    assert "2001" not in state["channels"] and "2001" not in state["items"]
    assert "2002" in state["channels"]


def test_expired_vod_is_forgotten_and_a_new_vod_is_ingested():
    fake = FakeTwitch()
    state = {}
    run(fake, state)
    fake.expire_video("1234569")
    fake.add_video("2001", VIDEO_RUST_NEW)
    rows, _ = run(fake, state)
    by_id = {row["id"]: row for row in rows}
    assert by_id["video:1234569"] == {"id": "video:1234569", "_deleted": True}
    assert by_id["video:1234570"]["_deleted"] is False
    assert "Building a compiler, part 4" in by_id["video:1234570"]["content"]
    assert sorted(state["items"]["2001"]) == ["video:1234567", "video:1234568", "video:1234570"]


def test_renamed_channel_keeps_its_key_and_updates_its_row():
    fake = FakeTwitch()
    state = {}
    run(fake, state)
    fake.rename_channel("2002", "codingcorner", "CodingCorner")
    rows, _ = run(fake, state)
    by_id = {row["id"]: row for row in rows}
    assert "channel:2002" in by_id  # same key: broadcaster ids are stable
    assert by_id["channel:2002"]["url"] == "https://www.twitch.tv/codingcorner"
    assert by_id["channel:2002"]["cognee_node_set"] == ["channel:codingcorner"]
    assert "CodingCorner" in by_id["channel:2002"]["content"]
    # its clips moved to the new login's node set too
    assert by_id["clip:RandomClip1"]["cognee_node_set"] == ["channel:codingcorner"]


def test_channels_filter_syncs_only_the_selected_follow():
    fake = FakeTwitch()
    state = {}
    rows, stats = run(fake, state, channels=["ruststreams"])
    assert ids(rows) == ["channel:2001", "video:1234567", "video:1234568", "video:1234569"]
    assert stats["skipped"] == 2
    # unselected but still followed: kept in state, never forgotten
    assert state["channels"] == {"2001": "ruststreams"}
    rows, _ = run(fake, state, channels=["ruststreams"])
    assert ids(rows) == ["channel:2001", "video:1234567", "video:1234568", "video:1234569"]


def test_an_unknown_login_in_the_selection_warns_and_syncs_nothing(monkeypatch):
    lines = []

    class RecordingLogger:
        def info(self, fmt, *args):
            lines.append(fmt % args)

        warning = info

    monkeypatch.setattr(module, "logger", RecordingLogger())
    fake = FakeTwitch()
    rows, stats = run(fake, {}, channels=["notafollow"])
    assert rows == []
    assert stats["skipped"] == 3
    assert any("notafollow" in line for line in lines)


def test_max_channels_bounds_the_run_to_the_most_recent_follows():
    fake = FakeTwitch()
    state = {}
    rows, stats = run(fake, state, max_channels=1)
    # follows are listed most recently followed first: CodeCorner (2025-06) leads
    assert ids(rows) == ["channel:2002", "clip:RandomClip1", "clip:RandomClip2"]
    assert stats["skipped"] == 2
    assert state["channels"] == {"2002": "codecorner"}


def test_a_failed_run_replays_in_full():
    fake = FakeTwitch()
    persisted = {}
    rows, _ = run(fake, dict(persisted))  # the load failed: dlt kept the old state
    assert ids(rows) == FIRST_RUN_IDS
    rows, _ = run(fake, persisted)
    assert ids(rows) == FIRST_RUN_IDS


def test_a_run_stopped_after_its_first_row_saves_no_state():
    fake = FakeTwitch()
    state = {}
    rows = sync_channels(fake, state, user=USER_DICT, stats={})
    next(rows)  # the first row is out
    rows.close()  # what happens when check_active raises between rows
    assert state == {}, "the stored keys only move once every row is out"


# --------------------------------------------------------------------- rate limits
def test_a_long_wait_stops_the_run_cleanly_and_the_next_run_replays():
    fake = FakeTwitch()
    fake.long_wait = ("videos", "2001")
    state = {}
    rows, stats = run(fake, state)
    assert rows == []
    assert stats["failed"] == 1 and stats["failed_rate_limit"] == 1
    assert stats["items"] == 0 and stats["deletions"] == 0
    assert state == {}, "nothing was stored, so the next run loses nothing"
    fake.long_wait = None
    rows, _ = run(fake, state)
    assert ids(rows) == FIRST_RUN_IDS


def test_an_auth_failure_mid_run_is_counted_and_raised():
    fake = FakeTwitch()
    fake.auth_fail_on = ("videos", "2001")
    stats = {}
    with pytest.raises(TwitchAuthError):
        list(sync_channels(fake, {}, user=USER_DICT, stats=stats))
    assert stats["failed_auth"] == 1 and stats["failed"] == 1


def _client(*outcomes):
    sleeps = []
    client = TwitchClient("client-id-value", TOKEN, sleep=sleeps.append)
    client._open = Opener(*outcomes)
    return client, sleeps


def test_a_429_with_a_short_wait_is_slept_and_retried():
    client, sleeps = _client(
        http_error(429, reset=in_seconds(5)), Response({"data": [], "pagination": {}})
    )
    assert client.get("videos", user_id="2001") == {"data": [], "pagination": {}}
    assert len(sleeps) == 1 and 0 < sleeps[0] <= 5


def test_a_429_with_a_long_wait_raises_without_sleeping():
    client, sleeps = _client(http_error(429, reset=in_seconds(3600)))
    with pytest.raises(TwitchRateLimitError) as info:
        client.get("videos", user_id="2001")
    assert info.value.retry_after == pytest.approx(3600, abs=5)
    assert sleeps == []


def test_low_remaining_points_back_off_to_the_reset():
    client, sleeps = _client(
        Response({"data": []}, remaining=10, reset=in_seconds(3)),
        Response({"data": []}),
    )
    assert client.get("users") == {"data": []}
    assert sleeps == []
    assert client.get("clips", broadcaster_id="2001") == {"data": []}
    assert len(sleeps) == 1 and 0 < sleeps[0] <= 3


def test_low_remaining_with_a_reset_beyond_a_minute_stops_before_the_request():
    opener = Opener(
        Response({"data": []}, remaining=5, reset=in_seconds(3600)),
        Response({"data": []}),
    )
    sleeps = []
    client = TwitchClient("client-id-value", TOKEN, sleep=sleeps.append)
    client._open = opener
    assert client.get("users") == {"data": []}
    with pytest.raises(TwitchRateLimitError) as info:
        client.get("clips", broadcaster_id="2001")
    assert info.value.retry_after == pytest.approx(3600, abs=5)
    assert sleeps == []
    assert len(opener.outcomes) == 1, "the second request was never made"


def test_a_429_without_a_reset_header_falls_back_to_backoff():
    client, sleeps = _client(http_error(429), Response({"data": []}))
    assert client.get("users") == {"data": []}
    assert sleeps == [1.0]


@pytest.mark.parametrize("blip", [ConnectionResetError(104, "reset"), TimeoutError()])
def test_a_dropped_connection_is_retried(blip):
    client, sleeps = _client(blip, Response({"data": []}))
    assert client.get("users") == {"data": []}
    assert sleeps == [1.0]


def test_a_5xx_is_retried():
    client, sleeps = _client(http_error(503), Response({"data": []}))
    assert client.get("users") == {"data": []}
    assert sleeps == [1.0]


class _RawResponse(io.BytesIO):
    """A 200 whose body is not JSON (a proxy's error page)."""

    def __init__(self, body):
        super().__init__(body)
        self.headers = Headers()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_a_200_without_json_is_a_twitch_error_without_the_token():
    client, _ = _client(_RawResponse(b"<html>bad gateway</html>"))
    with pytest.raises(TwitchAPIError) as info:
        client.get("users")
    assert TOKEN not in str(info.value)


# --------------------------------------------------------------------- errors
def test_a_401_raises_auth_error_and_never_carries_the_token():
    client, _ = _client(http_error(401, url=f"https://api.twitch.tv/helix/users?token={TOKEN}"))
    with pytest.raises(TwitchAuthError) as info:
        client.get("users")
    error = info.value
    assert TOKEN not in str(error) and "secret" not in str(error)
    assert error.__cause__ is None and error.__suppress_context__, "the HTTPError is hidden"


def test_the_client_repr_hides_the_token():
    client = TwitchClient("client-id-value", TOKEN)
    assert TOKEN not in repr(client)
    assert "secret" not in repr(client)
    assert "client-id-value" in repr(client)


def test_missing_credentials_say_where_to_get_them(monkeypatch):
    monkeypatch.setenv("TWITCH_ACCESS_TOKEN", "from-env")
    with pytest.raises(ValueError, match=r"dev\.twitch\.tv/console"):
        twitch_source()
    with pytest.raises(ValueError, match="access_token"):
        twitch_source(client_id="an-id")


def test_max_channels_must_be_positive():
    for bad in (0, -1, None):
        with pytest.raises(ValueError, match="max_channels"):
            twitch_source(client_id="id", access_token=TOKEN, client=FakeTwitch(), max_channels=bad)


def test_a_resource_name_dlt_would_rename_is_refused():
    with pytest.raises(ValueError, match="resource_name"):
        twitch_source(
            client_id="id", access_token=TOKEN, client=FakeTwitch(), resource_name="My-Twitch"
        )


def test_service_is_an_alias_for_client():
    fake = FakeTwitch()
    source = twitch_source(client_id="id", access_token=TOKEN, service=fake)
    assert len(list(source.resources["twitch_testuser"])) == 8


# --------------------------------------------------------------------- dlt wiring
def test_resource_is_configured_for_merge_hard_delete_and_node_sets():
    pytest.importorskip("dlt")
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR

    source = twitch_source(client_id="id", access_token=TOKEN, client=FakeTwitch())
    resource = source.resources["twitch_testuser"]
    assert resource.write_disposition == "merge"
    columns = resource.compute_table_schema()["columns"]
    assert columns["_deleted"]["hard_delete"] is True
    assert columns["cognee_node_set"]["data_type"] == "json"
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == "twitch"
    assert getattr(source, PIPELINE_SCOPE_ATTR) == "twitch:testuser"
    assert source.cognee_sync_stats is resource.cognee_sync_stats
    # a chosen name is the scope, so two users given one name share one state
    # (and the second is refused) instead of silently writing into one table
    named = twitch_source(
        client_id="id", access_token=TOKEN, client=FakeTwitch(), resource_name="mine"
    )
    assert getattr(named, PIPELINE_SCOPE_ATTR) == "twitch:mine"


def test_a_non_merge_disposition_is_refused():
    pytest.importorskip("dlt")
    source = twitch_source(client_id="id", access_token=TOKEN, client=FakeTwitch())
    resource = source.resources["twitch_testuser"]
    resource.write_disposition = "replace"
    # dlt wraps the refusal in its extraction error; the message is what matters
    with pytest.raises(Exception, match="merge"):
        list(resource)


def test_row_becomes_a_twitch_document_in_cognee():
    # The row -> document mapping is cognee's (the same check the telegram tests do).
    from types import SimpleNamespace
    from uuid import NAMESPACE_OID, uuid5

    from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

    row = video_to_row(VIDEO_RUST_ARCHIVE)
    data_id = uuid5(NAMESPACE_OID, row["id"])
    item = _build_document_data_item(
        SimpleNamespace(row_data=row, table_name="twitch_testuser"), data_id, "twitch"
    )
    assert item.system_metadata["source"] == "twitch"
    assert item.system_metadata["external_id"] == "video:1234567"
    assert item.system_metadata["url"] == "https://www.twitch.tv/videos/1234567"
    assert item.data.startswith("# Building a compiler, part 3")
    # the row's node set, namespaced under the source tag
    assert item.node_set == ["twitch:channel:ruststreams"]


def test_check_active_runs_before_every_helix_call():
    class AccessRevokedError(Exception):
        pass

    fake = FakeTwitch()
    checks = []

    def revoked_after_first_check():
        checks.append(1)
        if len(checks) > 1:
            raise AccessRevokedError

    source = twitch_source(
        client_id="id", access_token=TOKEN, client=fake, check_active=revoked_after_first_check
    )
    with pytest.raises(Exception) as caught:
        list(source.resources["twitch_testuser"])
    causes, error = [], caught.value
    while error is not None:
        causes.append(error)
        error = error.__cause__ or error.__context__
    assert any(isinstance(cause, AccessRevokedError) for cause in causes)
    assert [path for path, _ in fake.calls] == ["users"], "validation ran, nothing else did"


def test_stats_hold_counts_only():
    source = twitch_source(client_id="id", access_token=TOKEN, client=FakeTwitch())
    list(source.resources["twitch_testuser"])
    stats = source.cognee_sync_stats
    assert stats["items"] == 8 and stats["deletions"] == 0
    assert set(stats) == {"items", "deletions", "skipped", "failed"}
    assert all(type(value) is int for value in stats.values())
