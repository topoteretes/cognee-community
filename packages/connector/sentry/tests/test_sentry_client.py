"""Client tests: the Link paginator, auth, rate-limit pacing, retries and bad bodies."""

import re
import time

import httpx
import pytest
from fake_sentry import FakeSentry, ago, client_for, listing_calls, serve, sync

from cognee_community_connector_sentry import sentry
from cognee_community_connector_sentry.sentry import (
    SentryAPIError,
    SentryAuthError,
    SentryClient,
    SentryRateLimitedError,
    SentryUnavailableError,
    _iter_rows,
    next_cursor,
)


@pytest.fixture
def fake():
    return FakeSentry()


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def test_paginator_stops_on_results_false():
    """Sentry always sends a rel="next" link; only ``results`` says there is a page."""
    more = (
        '<https://sentry.io/api/0/issues/?&cursor=0:0:1>; rel="previous"; results="false"; '
        'cursor="0:0:1", '
        '<https://sentry.io/api/0/issues/?&cursor=0:100:0>; rel="next"; results="true"; '
        'cursor="0:100:0"'
    )
    last = (
        '<https://sentry.io/api/0/issues/?&cursor=0:0:1>; rel="previous"; results="true"; '
        'cursor="0:0:1", '
        '<https://sentry.io/api/0/issues/?&cursor=0:200:0>; rel="next"; results="false"; '
        'cursor="0:200:0"'
    )
    assert next_cursor(more) == "0:100:0"
    assert next_cursor(last) is None
    assert next_cursor(None) is None
    assert next_cursor("") is None


def test_a_next_link_without_a_cursor_raises():
    """Following it without a cursor would re-read the first page forever."""
    with pytest.raises(SentryAPIError, match="without a cursor"):
        next_cursor('<https://sentry.io/x>; rel="next"; results="true"')


def test_listing_walks_every_page_and_stops(fake):
    for index in range(250):
        fake.add_issue(f"i{index:03d}", last_seen=ago(index))
    state: dict = {}

    upserts, deletes, _ = sync(fake, state, event_sample=0)

    assert len(upserts) == 250
    assert not deletes
    cursors = [call[2].get("cursor") for call in listing_calls(fake)]
    # three pages of 100, and no fourth request after results="false"
    assert cursors == [None, "0:100:0", "0:200:0"]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_a_wrong_token_is_an_auth_error(fake):
    fake.add_issue("i1")
    client = SentryClient("wrong", org_slug=fake.org, http=fake.client(), sleep=lambda _s: None)
    with pytest.raises(SentryAuthError, match="HTTP 401"):
        client.get(client.issues_path)


def test_a_missing_token_fails_at_construction(fake):
    with pytest.raises(ValueError, match="auth token"):
        SentryClient("", org_slug=fake.org)
    with pytest.raises(ValueError, match="organization slug"):
        SentryClient("tok", org_slug="")


def test_a_403_names_the_scopes_the_token_needs(fake):
    fake.add_issue("i1")
    client = client_for(fake)
    undo = serve(fake, "/issues/", 403, b'{"detail": "You do not have permission"}')
    try:
        with pytest.raises(sentry.SentryAccessError, match="event:read"):
            client.get(client.issues_path)
    finally:
        undo()


# ---------------------------------------------------------------------------
# Bad bodies
# ---------------------------------------------------------------------------


def test_a_listing_that_is_not_an_array_raises(fake):
    """An HTML error page or an object body must never read as "no issues"."""
    undo = serve(fake, "/issues/", 200, b"<html>maintenance</html>")
    try:
        client = client_for(fake)
        with pytest.raises(SentryAPIError, match="non-JSON"):
            client.get_listing(client.issues_path)
    finally:
        undo()

    undo = serve(fake, "/issues/", 200, b'{"detail": "nope"}')
    try:
        client = client_for(fake)
        with pytest.raises(SentryAPIError, match="not a JSON array"):
            client.get_listing(client.issues_path)
    finally:
        undo()


def test_a_listing_of_non_objects_raises(fake):
    undo = serve(fake, "/issues/", 200, b'["i1", "i2"]')
    try:
        client = client_for(fake)
        with pytest.raises(SentryAPIError, match="non-object entries"):
            client.get_listing(client.issues_path)
    finally:
        undo()


def test_an_unrecognised_404_is_an_error_not_a_deletion(fake):
    """Only Sentry's "does not exist" body may be read as gone."""
    client = client_for(fake)
    undo = serve(fake, "/issues/i1/", 404, b'{"detail": "Invalid organization"}')
    try:
        with pytest.raises(SentryAPIError, match="unrecognised body"):
            client.get("/api/0/issues/i1/")
    finally:
        undo()


# ---------------------------------------------------------------------------
# Retries and rate limits
# ---------------------------------------------------------------------------


def test_server_errors_are_retried_then_raised(fake):
    fake.add_issue("i1")
    fake.queued = [httpx.Response(500, content=b"{}")] * 3
    sleeps: list[float] = []
    client = client_for(fake, sleeps)

    with pytest.raises(SentryUnavailableError, match="HTTP 500"):
        client.get(client.issues_path)

    assert sleeps == [1, 2]  # three attempts, backing off 1s then 2s


def test_a_429_is_waited_out_then_gives_up(fake):
    fake.add_issue("i1")
    reset = str(int(time.time()) + 3)
    rate_limited = httpx.Response(
        429,
        content=b'{"detail": "rate limit exceeded"}',
        headers={"X-Sentry-Rate-Limit-Reset": reset, "X-Sentry-Rate-Limit-Remaining": "0"},
    )
    fake.queued = [rate_limited]
    sleeps: list[float] = []
    client = client_for(fake, sleeps)
    client.get(client.issues_path)
    assert sleeps and 0 < sleeps[0] <= 3

    fake.queued = [rate_limited] * 5
    with pytest.raises(SentryRateLimitedError, match="kept rate limiting"):
        client.get(client.issues_path)


def test_a_used_up_window_is_paced_from_the_rate_limit_headers(fake):
    """``Remaining: 0`` means the *next* request would 429, so the wait is taken then."""
    fake.add_issue("i1")
    fake.rate_limit = {
        "X-Sentry-Rate-Limit-Remaining": "0",
        "X-Sentry-Rate-Limit-Reset": str(int(time.time()) + 2),
        "X-Sentry-Rate-Limit-Limit": "20",
    }
    sleeps: list[float] = []
    client = client_for(fake, sleeps)

    client.get(client.issues_path)
    assert sleeps == [], "the answer this request already produced is not thrown away"

    client.get(client.issues_path)
    assert sleeps and 0 < sleeps[0] <= 2


def test_a_reset_further_away_than_the_cap_cuts_the_run_short(fake):
    fake.add_issue("i1", last_seen=ago(2))
    fake.add_issue("i2", last_seen=ago(1))
    fake.rate_limit = {
        "X-Sentry-Rate-Limit-Remaining": "0",
        "X-Sentry-Rate-Limit-Reset": str(int(time.time()) + 3600),
    }
    state: dict = {}

    upserts, deletes, stats = sync(fake, state, event_sample=1)

    assert not deletes, "a rate-limited run must forget nothing"
    assert stats["failed_rate_limit"] == 1
    assert len(upserts) < 2, "the run stopped instead of waiting out the window"


def test_a_window_with_budget_left_is_not_paced(fake):
    fake.add_issue("i1")
    fake.rate_limit = {
        "X-Sentry-Rate-Limit-Remaining": "19",
        "X-Sentry-Rate-Limit-Reset": str(int(time.time()) + 60),
    }
    sleeps: list[float] = []
    client = client_for(fake, sleeps)
    client.get(client.issues_path)
    assert sleeps == []


def test_a_network_failure_is_retried_then_raised(fake):
    def refuse(_request):
        raise httpx.ConnectError("no route to host")

    sleeps: list[float] = []
    client = SentryClient(
        "tok",
        org_slug=fake.org,
        http=httpx.Client(transport=httpx.MockTransport(refuse)),
        sleep=sleeps.append,
    )
    with pytest.raises(SentryUnavailableError, match="could not be reached"):
        client.get(client.issues_path)
    assert sleeps == [1, 2]


# ---------------------------------------------------------------------------
# Hosts
# ---------------------------------------------------------------------------


def test_the_host_sets_the_data_region_or_a_self_hosted_url(fake):
    assert SentryClient("t", org_slug="o").base_url == "https://sentry.io"
    assert SentryClient("t", org_slug="o", host="de.sentry.io").base_url == "https://de.sentry.io"
    assert (
        SentryClient("t", org_slug="o", host="http://sentry.internal:9000/").base_url
        == "http://sentry.internal:9000"
    )
    with pytest.raises(ValueError, match="http"):
        SentryClient("t", org_slug="o", host="ftp://sentry.internal")


def test_two_organizations_never_share_sync_state(fake):
    other = FakeSentry(org="other-org")
    fake.add_issue("i1")
    other.add_issue("x1")
    state: dict = {}
    sync(fake, state, event_sample=0)

    with pytest.raises(sentry.SentrySourceError, match="another Sentry organization"):
        list(_iter_rows(client_for(other), state, {}, event_sample=0))


def test_a_repeating_page_cursor_is_an_error_not_an_endless_listing(fake):
    """A cursor Sentry hands back unchanged would re-read page 1 forever.

    Nothing is forgotten (the listing never completes, so no deletion pass runs), but
    the run burns its whole request budget on one page and every later run resumes on
    the same stuck cursor, so the listing never finishes again.
    """
    fake.add_issue("i1")
    fake.stuck_cursor = True
    with pytest.raises(SentryAPIError, match="same page cursor twice"):
        sync(fake, {}, event_sample=0)


def test_one_endpoints_spent_window_does_not_stall_another(fake):
    """Sentry meters per endpoint: issue detail 5 a window, the listing 10.

    Sharing one resume time across the client means spending the small detail budget
    blocks the listing as well, and the run then stops with a message naming the wrong
    endpoint.
    """
    fake.add_issue("i1")
    fake.rate_limit = {
        "X-Sentry-Rate-Limit-Remaining": "0",
        "X-Sentry-Rate-Limit-Reset": str(int(time.time()) + 3600),
    }
    fake.rate_limit_path = "/issues/i1/"
    sleeps: list[float] = []
    client = client_for(fake, sleeps)

    client.get(f"{sentry.API_ROOT}/issues/i1/")

    # the issue-detail window is now spent for an hour; the listing must still answer
    client.get(client.issues_path)
    assert sleeps == [], "the listing has its own budget and waits for nothing"
    with pytest.raises(SentryRateLimitedError, match=re.escape("/issues/{id}/")):
        client.get(f"{sentry.API_ROOT}/issues/i1/")


def test_many_short_rate_limit_waits_cannot_hold_the_run_for_hours(fake):
    """Each wait is capped at 60s, but nothing stopped them accumulating.

    Measured before the fix: a 41-request run slept 2,273s (38 minutes) across 39 waits,
    scaling linearly with ``max_requests`` - about 32 hours at the default of 2,000, all
    of it while cognee holds its process-wide dlt staging lock. That lock is the reason
    the per-wait cap exists, so the run needs a total budget too.
    """
    for n in range(40):
        fake.add_issue(f"i{n}", last_seen=ago(n + 1))
    # every answer says the window is spent and resets in 58s, just under the per-wait cap
    fake.rate_limit = {
        "X-Sentry-Rate-Limit-Remaining": "0",
        "X-Sentry-Rate-Limit-Reset": str(int(time.time()) + 58),
    }
    sleeps: list[float] = []
    state: dict = {}

    # event_sample=1 so each issue costs an event request, and each request a wait
    _, deletes, stats = sync(fake, state, sleeps, event_sample=1)

    assert not deletes, "a rate-limited run must forget nothing"
    assert stats["failed_rate_limit"] == 1, "it stops cleanly and resumes next run"
    assert sum(sleeps) <= sentry._MAX_TOTAL_WAIT, (
        f"the run slept {sum(sleeps):.0f}s, over the {sentry._MAX_TOTAL_WAIT:.0f}s budget"
    )
