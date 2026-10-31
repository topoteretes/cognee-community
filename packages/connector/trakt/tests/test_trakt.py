"""Unit tests for the Trakt connector.

The Trakt API is mocked by ``FakeTrakt`` (see fake_trakt.py; no token, no
network), so these run in CI. The HTTP-level behaviour of the real client
(retries, redaction, token refresh) is tested against a stubbed opener, like
the Telegram connector's tests. Coverage:

  - rendering: movie / episode / show / list rows, rating documents for items
    that were never watched, node sets, deterministic rows, timeline sorted
  - first sync pulls every section; a quiet second run pulls nothing
  - only the episode activity moving re-pulls the watched shows, never the
    movies, and never deletes unchanged movie rows
  - removing a movie from history -> ``_deleted`` row; a still-rated movie
    keeps its rating document; unwatched episodes are forgotten
  - ratings for unwatched items appear as documents
  - list pagination follows ``X-Pagination-Page-Count``
  - edits upsert the same key
  - a long Retry-After stops the run cleanly and the next run replays
  - a 401 raises TraktAuthError whose message (and the client repr) never
    contain the token
  - a sync state written by another user is refused
  - the OAuth PIN flow helper
  - dlt resource wiring: merge + id PK + hard_delete + node-set column +
    ``DOCUMENT_SOURCE_ATTR`` + stats; ``check_active`` before every call
"""

import io
import json
import urllib.error

import pytest
from fake_trakt import DARK, INCEPTION, LIST_REWATCHABLES, OLD_BOY, SEVERANCE, TENET, FakeTrakt

from cognee_community_connector_trakt.trakt import (
    TraktAuthError,
    TraktClient,
    TraktRateLimitError,
    episode_to_row,
    list_to_row,
    movie_to_row,
    rating_to_row,
    show_to_row,
    sync_trakt,
    trakt_pin_flow,
    trakt_source,
)

TOKEN = "secret-access-token-value"
CLIENT_SECRET = "secret-client-value"
SEVERANCE_INDEX = {
    (1, 1): (101, "Good News About Hell"),
    (1, 2): (102, "Half Loop"),
    (2, 1): (201, "Hello, Ms. Huang"),
    (2, 4): (204, "Woe's Hollow"),
}


def run(fake, state, **kwargs):
    stats = kwargs.pop("stats", {})
    rows = list(sync_trakt(fake, state, stats, **kwargs))
    return rows, stats


def rows_by_id(rows):
    return {row["id"]: row for row in rows}


# --------------------------------------------------------------------- rendering
def test_movie_row_embeds_rating_comment_watchlist_and_lists():
    context = {
        "ratings": {"movies": {"movie:1": {"rating": 9, "rated_at": "2021-06-01T12:00:00.000Z"}}},
        "comments": {"movies": {"movie:1": "Rewatched with the kids."}},
        "watchlist": ["movie:1"],
        "list_items": {"movie:1": ["rewatchables"]},
    }
    entry = {
        "plays": 2,
        "last_watched_at": "2024-11-02T21:10:00.000Z",
        "movie": INCEPTION,
    }
    row = movie_to_row(entry, context)
    assert row["id"] == "movie:1"
    assert row["title"] == "Inception (2010)"
    assert row["url"] == "https://trakt.tv/movies/inception-2010"
    assert row["_deleted"] is False
    assert row["cognee_node_set"] == ["list:rewatchables", "movie:inception-2010", "watchlist"]
    for line in (
        "Movie: Inception (2010)",
        "Runtime: 148 minutes",
        "My rating: 9/10 (rated 2021-06-01)",
        "Watched: 2 plays, last watched 2024-11-02",
        "My comment: Rewatched with the kids.",
        "On my watchlist: yes",
        "On my lists: rewatchables",
        "Overview: A thief who steals corporate secrets",
    ):
        assert line in row["content"]


def test_unrated_watched_movie_has_no_rating_line():
    entry = {"plays": 1, "last_watched_at": "2025-03-10T19:00:00.000Z", "movie": TENET}
    row = movie_to_row(entry, {})
    assert "My rating" not in row["content"]
    assert "Watched: 1 play, last watched 2025-03-10" in row["content"]
    assert "On my watchlist: yes" not in row["content"]


def test_show_row_timeline_is_sorted_by_watch_date():
    entry = FakeTrakt().watched_shows[0]  # season 2 lists E1 (2026) before E4 (2025)
    row = show_to_row(entry, SEVERANCE_INDEX, {"list_items": {"show:10": ["rewatchables"]}})
    assert row["id"] == "show:10"
    assert row["url"] == "https://trakt.tv/shows/severance"
    assert row["cognee_node_set"] == ["list:rewatchables", "show:severance"]
    timeline = [line for line in row["content"].splitlines() if line.startswith("- S")]
    assert timeline == [
        '- S1E1 "Good News About Hell" — 2025-02-01',
        '- S1E2 "Half Loop" — 2025-02-03',
        "- S2E4 \"Woe's Hollow\" — 2025-12-20 (2 plays)",
        '- S2E1 "Hello, Ms. Huang" — 2026-01-15',
    ]
    assert "On my lists: rewatchables" in row["content"]


def test_episode_row_has_show_season_node_sets_and_title():
    context = {"ratings": {"episodes": {"episode:201": {"rating": 8, "rated_at": "2026-01-16"}}}}
    episode = {"number": 1, "plays": 1, "last_watched_at": "2026-01-15T23:00:00.000Z"}
    row = episode_to_row(SEVERANCE, 2, episode, SEVERANCE_INDEX, context)
    assert row["id"] == "episode:201"
    assert row["title"] == 'Severance (2022) S2E1 "Hello, Ms. Huang"'
    assert row["url"] == "https://trakt.tv/shows/severance/seasons/2/episodes/1"
    assert row["cognee_node_set"] == ["season:severance-s2", "show:severance"]
    assert row["content"] == "\n".join(
        [
            "Show: Severance (2022)",
            'Episode: S2E1 "Hello, Ms. Huang"',
            "Watched: 1 play, last watched 2026-01-15",
            "My rating: 8/10 (rated 2026-01-16)",
        ]
    )


def test_rating_rows_for_unwatched_items_of_every_media_type():
    movie = rating_to_row(
        {"rated_at": "2020-02-02T12:00:00.000Z", "rating": 7, "movie": OLD_BOY}, {}
    )
    assert movie["id"] == "movie:2"
    assert "My rating: 7/10 (rated 2020-02-02)" in movie["content"]
    assert "Status: rated, but not watched yet" in movie["content"]
    assert movie["url"] == "https://trakt.tv/movies/oldboy-2003"

    show = rating_to_row({"rated_at": "2024-01-01T12:00:00.000Z", "rating": 6, "show": DARK}, {})
    assert show["id"] == "show:11"
    assert show["cognee_node_set"] == ["show:dark"]

    season = rating_to_row(
        {
            "rated_at": "2024-05-01T12:00:00.000Z",
            "rating": 8,
            "season": {"number": 1, "ids": {"trakt": 901, "tvdb": 5001}},
            "show": SEVERANCE,
        },
        {},
    )
    assert season["id"] == "season:901"
    assert season["cognee_node_set"] == ["season:severance-s1", "show:severance"]
    assert season["url"] == "https://trakt.tv/shows/severance/seasons/1"


def test_list_row_roster_is_ordered_by_rank():
    from fake_trakt import LIST_ITEMS_REWATCHABLES

    row = list_to_row(LIST_REWATCHABLES, LIST_ITEMS_REWATCHABLES)
    assert row["id"] == "list:rewatchables"
    assert row["title"] == "Rewatchables"
    assert row["url"] == "https://trakt.tv/lists/100"
    assert row["cognee_node_set"] == ["list:rewatchables"]
    roster = [line for line in row["content"].splitlines() if line.startswith("- ")]
    assert roster == [
        "- 1. Inception (2010) [movie] — added 2021-05-01",
        "- 2. Severance (2022) [show] — added 2021-05-02",
        '- 3. Severance (2022) S2E4 "Woe\'s Hollow" [episode] — added 2026-02-01',
    ]


def test_rows_are_deterministic():
    entry = {"plays": 2, "last_watched_at": "2024-11-02T21:10:00.000Z", "movie": INCEPTION}
    assert movie_to_row(entry, {}) == movie_to_row(dict(entry), {})
    show_entry = FakeTrakt().watched_shows[0]
    assert show_to_row(show_entry, SEVERANCE_INDEX, {}) == show_to_row(
        dict(show_entry), SEVERANCE_INDEX, {}
    )


# --------------------------------------------------------------------- first sync
def test_first_run_pulls_every_section_and_writes_state():
    fake = FakeTrakt()
    state = {}
    rows, stats = run(fake, state)
    assert sorted(rows_by_id(rows)) == [
        "episode:101",
        "episode:102",
        "episode:201",
        "episode:204",
        "list:rewatchables",
        "movie:1",
        "movie:2",
        "movie:3",
        "show:10",
        "show:11",
    ]
    assert stats["items"] == 10 and stats["deletions"] == 0 and stats["skipped"] == 0
    assert stats["requests"] == 15, "every section, plus the seasons call and list page 2"
    assert fake.paths().count("/sync/ratings/movies") == 1
    assert "/users/me/comments/movies" in fake.paths()
    assert state["owner"] == "demo"
    assert state["known"]["watched_movies"] == ["movie:1", "movie:3"]
    assert sorted(state["known"]["watched_shows"]) == [
        "episode:101",
        "episode:102",
        "episode:201",
        "episode:204",
        "show:10",
    ]
    assert state["known"]["ratings_movies"] == ["movie:2"], "rated but unwatched only"
    assert state["known"]["ratings_shows"] == ["show:11"]
    assert state["known"]["lists"] == ["list:rewatchables"]
    assert state["context"]["watchlist"] == ["episode:204", "movie:3"]
    assert state["context"]["list_items"]["movie:1"] == ["rewatchables"]
    assert state["activities"] == fake.activities


def test_a_quiet_second_run_pulls_nothing_and_emits_no_rows():
    fake = FakeTrakt()
    state = {}
    run(fake, state)
    fake.requests_log.clear()
    rows, stats = run(fake, state)
    assert rows == []
    assert fake.paths() == ["/users/me/settings", "/sync/last_activities"]
    assert stats["requests"] == 2 and stats["items"] == 0


def test_only_episode_activity_repulls_shows_not_movies_and_keeps_movies():
    fake = FakeTrakt()
    state = {}
    run(fake, state)
    fake.bump("episodes", "watched_at")  # an episode was watched somewhere
    fake.requests_log.clear()
    rows, stats = run(fake, state)
    assert "/users/me/watched/shows" in fake.paths()
    assert "/users/me/watched/movies" not in fake.paths()
    assert "/sync/watchlist" not in fake.paths(), "no secondary section moved"
    ids = rows_by_id(rows)
    assert "movie:1" not in ids and "movie:3" not in ids, "unchanged movies are not re-emitted"
    assert all(not row["_deleted"] for row in rows), "an unchanged section never deletes"
    assert ids["show:10"]["_deleted"] is False
    assert stats["deletions"] == 0
    # the show's episodes and the cached rating documents are re-emitted
    assert {"show:10", "episode:101", "episode:102", "episode:201", "episode:204"} <= set(ids)
    assert "movie:2" in ids and "show:11" in ids


# --------------------------------------------------------------------- deletions
def test_unwatching_a_movie_forgets_it():
    fake = FakeTrakt()
    state = {}
    run(fake, state)
    fake.unwatch_movie(TENET["ids"]["trakt"])  # gone from history, only watchlisted
    rows, stats = run(fake, state)
    ids = rows_by_id(rows)
    assert ids["movie:3"] == {"id": "movie:3", "_deleted": True}
    assert "movie:3" not in state["known"]["watched_movies"]
    assert ids["movie:1"]["_deleted"] is False, "a movie still watched stays"
    assert stats["deletions"] == 1


def test_unwatching_a_movie_that_is_still_rated_keeps_the_rating_document():
    fake = FakeTrakt()
    state = {}
    run(fake, state)
    fake.ratings["movies"].append(
        {"rated_at": "2025-04-01T12:00:00.000Z", "rating": 8, "movie": TENET}
    )
    fake.bump("movies", "rated_at")
    rows, _ = run(fake, state)
    assert "My rating: 8/10" in rows_by_id(rows)["movie:3"]["content"]

    fake.unwatch_movie(TENET["ids"]["trakt"])  # the rating survives the unwatch
    rows, stats = run(fake, state)
    ids = rows_by_id(rows)
    assert "movie:3" in ids and ids["movie:3"]["_deleted"] is False
    assert "Status: rated, but not watched yet" in ids["movie:3"]["content"]
    assert stats["deletions"] == 0


def test_unwatching_a_season_forgets_its_unrated_episodes():
    fake = FakeTrakt()
    state = {}
    run(fake, state)
    fake.unwatch_show_season(SEVERANCE["ids"]["trakt"], 2)
    rows, stats = run(fake, state)
    ids = rows_by_id(rows)
    assert ids["episode:204"] == {"id": "episode:204", "_deleted": True}
    assert stats["deletions"] == 1
    # episode 201 was rated, so it keeps a rating document instead of a tombstone
    assert ids["episode:201"]["_deleted"] is False
    assert "Status: rated, but not watched yet" in ids["episode:201"]["content"]
    # the show document survives with only season 1 left
    assert ids["show:10"]["_deleted"] is False
    assert "S2E4" not in ids["show:10"]["content"]
    assert '- S1E1 "Good News About Hell" — 2025-02-01' in ids["show:10"]["content"]


def test_a_failed_run_replays_the_same_work():
    fake = FakeTrakt()
    persisted = {}
    rows, _ = run(fake, dict(persisted))  # the load fails: dlt keeps the old state
    assert len(rows) == 10
    rows, _ = run(fake, persisted)
    assert len(rows) == 10, "nothing was confirmed, so everything is read again"


# --------------------------------------------------------------------- upserts
def test_rewatching_upserts_the_same_movie_row():
    fake = FakeTrakt()
    state = {}
    first, _ = run(fake, state)
    assert "Watched: 2 plays" in rows_by_id(first)["movie:1"]["content"]
    fake.watched_movies[0]["plays"] = 3
    fake.bump("movies", "watched_at")
    rows, _ = run(fake, state)
    movie_rows = [row for row in rows if row["id"] == "movie:1"]
    assert len(movie_rows) == 1, "the same key upserts, never a second row"
    assert "Watched: 3 plays" in movie_rows[0]["content"]


# --------------------------------------------------------------------- pagination
def test_list_items_follow_the_pagination_header():
    fake = FakeTrakt()
    state = {}
    rows, _ = run(fake, state)
    pages = fake.params_of("/users/me/lists/100/items")
    assert [p["page"] for p in pages] == [1, 2], "3 items, 2 per page: page 2 is read"
    roster = rows_by_id(rows)["list:rewatchables"]["content"]
    assert "Inception (2010) [movie]" in roster
    assert 'S2E4 "Woe\'s Hollow" [episode]' in roster


# --------------------------------------------------------------------- errors
def test_a_state_written_by_another_user_is_refused():
    fake = FakeTrakt()
    state = {}
    run(fake, state)
    other = FakeTrakt(username="someone-else")
    with pytest.raises(ValueError, match="another Trakt user"):
        run(other, state)
    assert other.paths() == ["/users/me/settings"], "no listing was ever read"


def test_a_401_is_counted_and_raised():
    fake = FakeTrakt()
    fake.fail_once = ("auth", 0)
    state, stats = {}, {}
    with pytest.raises(TraktAuthError):
        list(sync_trakt(fake, state, stats))
    assert stats["failed_auth"] == 1 and stats["failed"] == 1
    assert state == {}, "nothing was confirmed"


def test_a_long_retry_after_stops_cleanly_and_the_next_run_replays():
    fake = FakeTrakt()
    fake.fail_once = ("rate_limit", 3600)
    state, stats = {}, {}
    rows = list(sync_trakt(fake, state, stats))
    assert rows == []
    assert stats["failed_rate_limit"] == 1 and stats["failed"] == 1
    assert all(isinstance(value, int) for value in stats.values())
    assert state == {}, "no rows, no state change: the next run resumes"
    rows, stats = run(fake, state, stats=stats)
    assert len(rows) == 10 and stats["failed_rate_limit"] == 0


# --------------------------------------------------------------------- the real client
class _Opener:
    """A stubbed ``urlopen``: serves queued outcomes, records requests."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.seen = []

    def __call__(self, request):
        self.seen.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        body, headers = outcome
        return _FakeResponse(body, headers)


class _FakeResponse(io.BytesIO):
    def __init__(self, body, headers=None):
        super().__init__(body if isinstance(body, bytes) else json.dumps(body).encode())
        self.headers = {"Content-Type": "application/json", **(headers or {})}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _http_error(status, headers=None, url="https://api.trakt.tv/sync/last_activities"):
    return urllib.error.HTTPError(url, status, "err", headers or {}, io.BytesIO(b"{}"))


def _client(*outcomes, **kwargs):
    """A real TraktClient against queued outcomes: exceptions, or (body, headers)."""
    sleeps = []
    client = TraktClient("client-id", TOKEN, sleep=sleeps.append, **kwargs)
    client._open = _Opener(*outcomes)
    return client, sleeps


def test_a_429_with_a_short_retry_after_is_slept_through_and_retried():
    client, sleeps = _client(
        _http_error(429, {"Retry-After": "5"}),
        ({"all": "2025"}, None),
    )
    assert client.request("/sync/last_activities").json == {"all": "2025"}
    assert sleeps == [5.0]


def test_a_429_with_a_long_retry_after_stops_without_sleeping():
    client, sleeps = _client(_http_error(429, {"Retry-After": "3600"}))
    with pytest.raises(TraktRateLimitError) as info:
        client.request("/sync/last_activities")
    assert info.value.retry_after == 3600 and sleeps == []


def test_a_5xx_is_retried_with_backoff():
    client, sleeps = _client(_http_error(502), ({"ok": True}, None))
    assert client.request("/users/me/settings").json == {"ok": True}
    assert sleeps == [1.0]


def test_a_401_raises_auth_error_that_never_carries_the_token():
    client, _ = _client(_http_error(401, url=f"https://api.trakt.tv/x?token={TOKEN}"))
    with pytest.raises(TraktAuthError) as info:
        client.request("/users/me/settings")
    error = info.value
    assert TOKEN not in str(error) and "secret" not in str(error)
    assert error.__cause__ is None and error.__suppress_context__, "the HTTPError (URL) is hidden"


def test_the_client_repr_hides_the_token():
    assert TOKEN not in repr(TraktClient("client-id", TOKEN))
    assert "secret" not in repr(TraktClient("client-id", TOKEN, client_secret=CLIENT_SECRET))
    assert TOKEN not in repr(trakt_source(client_id="client-id", access_token=TOKEN))


def test_an_expired_token_is_refreshed_and_the_new_one_used():
    token_response = (
        {
            "access_token": "new-token",
            "token_type": "bearer",
            "expires_in": 604800,
            "refresh_token": "refresh-2",
            "scope": "public",
            "created_at": 5000,
        },
        None,
    )
    client, sleeps = _client(
        token_response,
        ({"user": {"username": "demo"}}, None),
        ({"all": "2025"}, None),
        refresh_token="refresh-1",
        client_secret=CLIENT_SECRET,
        token_created_at=1000,
        token_expires_in=100,
        now=lambda: 5000.0,
    )
    assert client.request("/users/me/settings").json["user"]["username"] == "demo"
    assert sleeps == [], "the refresh itself is not a retry loop"
    refresh = client._open.seen[0]
    assert refresh.full_url == "https://trakt.tv/oauth/token"
    assert json.loads(refresh.data)["grant_type"] == "refresh_token"
    assert client.token_info() == {
        "access_token": "new-token",
        "refresh_token": "refresh-2",
        "created_at": 5000,
        "expires_in": 604800,
    }
    client.request("/sync/last_activities")  # not expired any more: no refresh
    assert len(client._open.seen) == 3
    assert client._open.seen[2].get_header("Authorization") == "Bearer new-token"


def test_an_expired_token_without_refresh_material_raises_a_clear_error():
    client, _ = _client(
        refresh_token=None,
        client_secret=None,
        token_created_at=1000,
        token_expires_in=100,
        now=lambda: 5000.0,
    )
    with pytest.raises(TraktAuthError, match="trakt_pin_flow"):
        client.request("/sync/last_activities")


def test_adopt_token_takes_newer_material_only():
    client = TraktClient("client-id", "old", token_created_at=1000)
    assert client.adopt_token({"access_token": "new", "created_at": 2000}) is True
    assert client.token_info()["access_token"] == "new"
    assert client.adopt_token({"access_token": "ancient", "created_at": 1500}) is False
    assert client.token_info()["access_token"] == "new"


def test_the_pin_flow_prints_the_url_and_exchanges_the_pin():
    opener = _Opener(({"access_token": TOKEN, "expires_in": 604800, "created_at": 5000}, None))
    printed = []
    token = trakt_pin_flow(
        "client-id",
        CLIENT_SECRET,
        prompt=lambda _text: "12345678",
        opener=opener,
        print_token=printed.append,
    )
    assert token["access_token"] == TOKEN
    url = next(line for line in printed if "authorize" in line)
    assert "response_type=pin" in url and "client-id" in url
    body = json.loads(opener.seen[0].data)
    assert body == {
        "code": "12345678",
        "client_id": "client-id",
        "client_secret": CLIENT_SECRET,
        "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
        "grant_type": "authorization_code",
    }


# --------------------------------------------------------------------- dlt wiring
def test_missing_credentials_say_where_to_get_them():
    with pytest.raises(ValueError, match="trakt_pin_flow"):
        trakt_source()
    with pytest.raises(ValueError, match="client_id"):
        trakt_source(access_token=TOKEN)


@pytest.mark.parametrize("name", ["Trakt-Items", "trakt items", "1trakt"])
def test_a_resource_name_dlt_would_rename_is_refused(name):
    with pytest.raises(ValueError, match="resource_name"):
        trakt_source(
            client_id="client-id", access_token=TOKEN, client=FakeTrakt(), resource_name=name
        )


def test_resource_is_configured_for_merge_hard_delete_and_node_sets():
    pytest.importorskip("dlt")
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR

    source = trakt_source(client=FakeTrakt())
    resource = source.resources["trakt_items"]
    assert resource.write_disposition == "merge"
    columns = resource.compute_table_schema()["columns"]
    assert columns["_deleted"]["hard_delete"] is True
    assert columns["cognee_node_set"]["data_type"] == "json"
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == "trakt"
    assert getattr(source, PIPELINE_SCOPE_ATTR) == "trakt"
    scoped = trakt_source(client=FakeTrakt(), username="demo")
    assert getattr(scoped, PIPELINE_SCOPE_ATTR) == "trakt:demo"
    assert set(scoped.resources) == {"trakt_demo"}


def test_stats_are_shared_between_source_and_resource():
    source = trakt_source(client=FakeTrakt())
    source.cognee_sync_stats["items"] = 7
    assert source.resources["trakt_items"].cognee_sync_stats["items"] == 7


def test_row_becomes_a_trakt_document_in_cognee():
    from types import SimpleNamespace
    from uuid import NAMESPACE_OID, uuid5

    from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

    entry = {"plays": 2, "last_watched_at": "2024-11-02T21:10:00.000Z", "movie": INCEPTION}
    row = movie_to_row(entry, {})
    data_id = uuid5(NAMESPACE_OID, row["id"])
    item = _build_document_data_item(
        SimpleNamespace(row_data=row, table_name="trakt_items"), data_id, "trakt"
    )
    assert item.system_metadata["source"] == "trakt"
    assert item.system_metadata["external_id"] == "movie:1"
    assert item.system_metadata["url"] == "https://trakt.tv/movies/inception-2010"
    assert item.data.startswith("# Inception (2010)")
    assert "Watched: 2 plays" in item.data


def test_check_active_runs_before_every_request_and_around_every_row():
    fake = FakeTrakt()
    checks = []
    source = trakt_source(client=fake, check_active=lambda: checks.append(1))
    rows = list(source.resources["trakt_items"])
    assert len(rows) == 10
    # once before each of the API calls, then around each row and twice at the end
    assert len(checks) == len(fake.requests_log) + 2 * len(rows) + 2


def test_revoked_access_stops_the_sync_before_any_listing_call():
    class AccessRevokedError(Exception):
        pass

    fake = FakeTrakt()

    def revoked_after_first_request():
        if fake.requests_log:
            raise AccessRevokedError

    source = trakt_source(client=fake, check_active=revoked_after_first_request)
    with pytest.raises(Exception) as caught:
        list(source.resources["trakt_items"])
    causes, error = [], caught.value
    while error is not None:
        causes.append(error)
        error = error.__cause__ or error.__context__
    assert any(isinstance(cause, AccessRevokedError) for cause in causes)
    assert fake.paths() == ["/users/me/settings"], "no listing was ever read"
