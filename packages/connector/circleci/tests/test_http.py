"""HTTP layer: auth header, pagination and retries, against recorded API responses."""

import pytest
import requests

from cognee_community_connector_circleci.circleci import (
    _MAX_RETRIES,
    DEFAULT_BASE_URL,
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


def test_retries_429_using_retry_after(fake_session, no_sleep):
    fake_session.queue(PIPELINES, (429, {}, {"Retry-After": "2"}))

    data = _api_get(fake_session, DEFAULT_BASE_URL, PIPELINES)

    assert len(data["items"]) == 5
    assert no_sleep == [2.0]


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

    with pytest.raises(requests.HTTPError):
        _api_get(fake_session, DEFAULT_BASE_URL, PIPELINES)
    assert len(no_sleep) == _MAX_RETRIES - 1


def test_404_raises_at_once(fake_session, no_sleep):
    missing = "/project/gh/rokadepiyush49-rgb/does-not-exist/pipeline"

    with pytest.raises(requests.HTTPError) as excinfo:
        _api_get(fake_session, DEFAULT_BASE_URL, missing)

    assert excinfo.value.response.status_code == 404
    assert no_sleep == []
