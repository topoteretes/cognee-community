"""HTTP layer: auth header, pagination and retries, against recorded API responses."""

import pytest
import requests

from cognee_community_connector_circleci.circleci import (
    _MAX_RETRIES,
    _MAX_RETRY_AFTER_SECONDS,
    DEFAULT_BASE_URL,
    CircleCIAPIError,
    CircleCIAuthError,
    CircleCINotFoundError,
    CircleCIRateLimitedError,
    _ActiveCheckedSession,
    _api_get,
    _make_session,
    _paginate,
)

SLUG = "gh/rokadepiyush49-rgb/cognee-circleci-fixture"
PIPELINES = f"/project/{SLUG}/pipeline"


def test_session_authenticates_with_circle_token_header():
    session = _make_session("secret-token")
    assert session.headers["Circle-Token"] == "secret-token"


def test_paginate_reads_recorded_pipeline_list(fake_session):
    pipelines = list(_paginate(fake_session, DEFAULT_BASE_URL, PIPELINES))

    assert [p["number"] for p in pipelines] == [5, 4, 3, 2, 1]
    assert {p["vcs"]["branch"] for p in pipelines} == {
        "main",
        "green",
        "many-failures",
        "slow",
        "hold",
    }


def test_paginate_follows_next_page_token(fake_session):
    fake_session.queue(
        "/things",
        (200, {"items": [{"n": 1}], "next_page_token": "abc"}),
        (200, {"items": [{"n": 2}], "next_page_token": None}),
    )

    items = list(_paginate(fake_session, DEFAULT_BASE_URL, "/things", {"branch": "main"}))

    assert items == [{"n": 1}, {"n": 2}]
    assert [params for _, params in fake_session.calls] == [
        {"branch": "main"},
        {"branch": "main", "page-token": "abc"},
    ]


def test_paginate_is_lazy(fake_session):
    # The sync stops at the cursor, so later pages must not be fetched up front.
    fake_session.queue("/things", (200, {"items": [{"n": 1}], "next_page_token": "abc"}))

    assert next(_paginate(fake_session, DEFAULT_BASE_URL, "/things")) == {"n": 1}
    assert len(fake_session.calls) == 1


def test_paginate_raises_when_the_page_token_repeats(fake_session):
    fake_session.queue(
        "/things",
        (200, {"items": [{"n": 1}], "next_page_token": "abc"}),
        (200, {"items": [{"n": 2}], "next_page_token": "abc"}),
    )

    with pytest.raises(CircleCIAPIError, match="did not advance"):
        list(_paginate(fake_session, DEFAULT_BASE_URL, "/things"))
    assert len(fake_session.calls) == 2


def test_retries_429_using_retry_after(fake_session, no_sleep):
    fake_session.queue(PIPELINES, (429, {}, {"Retry-After": "2"}))

    data = _api_get(fake_session, DEFAULT_BASE_URL, PIPELINES)

    assert len(data["items"]) == 5
    assert no_sleep == [2.0]


def test_retry_after_is_capped(fake_session, no_sleep):
    # A long Retry-After would hold cognee's staging lock for the whole wait.
    fake_session.queue(PIPELINES, (429, {}, {"Retry-After": "900"}))

    _api_get(fake_session, DEFAULT_BASE_URL, PIPELINES)

    assert no_sleep == [_MAX_RETRY_AFTER_SECONDS]


def test_retries_5xx_with_exponential_backoff(fake_session, no_sleep):
    fake_session.queue(PIPELINES, (503, {}), (502, {}))

    _api_get(fake_session, DEFAULT_BASE_URL, PIPELINES)

    assert no_sleep == [1.0, 2.0]


def test_retries_connection_errors(fake_session, no_sleep):
    fake_session.queue(PIPELINES, requests.ConnectionError("connection reset"))

    _api_get(fake_session, DEFAULT_BASE_URL, PIPELINES)

    assert no_sleep == [1.0]


def test_gives_up_after_max_retries(fake_session, no_sleep):
    fake_session.queue(PIPELINES, *[(503, {})] * _MAX_RETRIES)

    with pytest.raises(CircleCIAPIError) as excinfo:
        _api_get(fake_session, DEFAULT_BASE_URL, PIPELINES)
    assert excinfo.value.status == 503
    assert len(no_sleep) == _MAX_RETRIES - 1


def test_rate_limit_that_never_clears_raises_rate_limited(fake_session, no_sleep):
    fake_session.queue(PIPELINES, *[(429, {})] * _MAX_RETRIES)

    with pytest.raises(CircleCIRateLimitedError):
        _api_get(fake_session, DEFAULT_BASE_URL, PIPELINES)


def test_unreachable_api_error_does_not_carry_the_url(fake_session, no_sleep):
    error = requests.ConnectionError(
        f"Max retries exceeded with url: {DEFAULT_BASE_URL}{PIPELINES}"
    )
    fake_session.queue(PIPELINES, *[error] * _MAX_RETRIES)

    with pytest.raises(CircleCIAPIError) as excinfo:
        _api_get(fake_session, DEFAULT_BASE_URL, PIPELINES)

    assert str(excinfo.value) == "CircleCI could not be reached (ConnectionError)"
    assert excinfo.value.__cause__ is None
    assert excinfo.value.path == PIPELINES


def test_404_raises_at_once_with_the_path_on_the_error_only(fake_session, no_sleep):
    missing = "/project/gh/rokadepiyush49-rgb/does-not-exist/pipeline"

    with pytest.raises(CircleCINotFoundError) as excinfo:
        _api_get(fake_session, DEFAULT_BASE_URL, missing)

    assert excinfo.value.status == 404
    assert excinfo.value.path == missing
    assert "does-not-exist" not in str(excinfo.value)
    assert no_sleep == []


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors_are_named(fake_session, status):
    fake_session.queue(PIPELINES, (status, {"message": "Invalid token provided."}))

    with pytest.raises(CircleCIAuthError, match=f"HTTP {status}"):
        _api_get(fake_session, DEFAULT_BASE_URL, PIPELINES)


def test_check_active_runs_before_every_request(fake_session):
    checks = []
    session = _ActiveCheckedSession(fake_session, lambda: checks.append(len(fake_session.calls)))

    _api_get(session, DEFAULT_BASE_URL, PIPELINES)
    _api_get(session, DEFAULT_BASE_URL, PIPELINES)

    # Called before each request went out.
    assert checks == [0, 1]


def test_check_active_can_stop_the_request(fake_session):
    def revoked():
        raise PermissionError("connection revoked")

    with pytest.raises(PermissionError):
        _api_get(_ActiveCheckedSession(fake_session, revoked), DEFAULT_BASE_URL, PIPELINES)
    assert fake_session.calls == []
