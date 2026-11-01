"""The Rollbar HTTP client: auth header, paging, retries and how errors are told apart."""

import httpx
import pytest
from fake_rollbar import PROJECT, TOKEN, FakeRollbar, http_error

from cognee_community_connector_rollbar.rollbar import (
    RollbarAPIError,
    RollbarAuthError,
    RollbarClient,
    RollbarRateLimitedError,
)

ALL = ("active", "resolved", "muted")


def _client(fake: FakeRollbar, token: str = TOKEN) -> tuple[RollbarClient, list[float]]:
    sleeps: list[float] = []
    return RollbarClient(fake.client(), token, sleep=sleeps.append), sleeps


def test_the_token_travels_in_the_header_never_in_the_url():
    fake = FakeRollbar()
    fake.add_item("KeyError: 'plan_tier'")
    client, _ = _client(fake)

    client.list_items(PROJECT, statuses=ALL)

    request = fake.requests[0]
    assert request.headers["X-Rollbar-Access-Token"] == TOKEN
    assert TOKEN not in str(request.url)


def test_filters_are_sent_as_repeated_parameters():
    fake = FakeRollbar()
    client, _ = _client(fake)

    client.list_items(
        PROJECT, statuses=("active", "muted"), environments=("production",), levels=("error",)
    )

    params = fake.requests[0].url.params
    assert params.get_list("status") == ["active", "muted"]
    assert params.get_list("environment") == ["production"]
    assert params.get_list("level") == ["error"]
    assert params.get_list("project_ids") == [str(PROJECT)]


def test_listing_follows_pages_until_an_empty_one():
    fake = FakeRollbar(page_size=2)
    for number in range(5):
        fake.add_item(f"Error {number}")
    client, _ = _client(fake)

    items = client.list_items(PROJECT, statuses=ALL)

    assert sorted(item["title"] for item in items) == [f"Error {n}" for n in range(5)]
    assert [r.url.params["page"] for r in fake.requests] == ["1", "2", "3", "4"]


def test_an_item_repeated_on_the_next_page_is_kept_once():
    fake = FakeRollbar(page_size=2)
    for number in range(3):
        fake.add_item(f"Error {number}")
    client, _ = _client(fake)
    # a new item before page 2 shifts the listing, so page 2 repeats one item
    fake.before_page = lambda page: page == 2 and fake.add_item("Arrived mid-listing")

    items = client.list_items(PROJECT, statuses=ALL)

    assert len(items) == len({item["id"] for item in items})


def test_a_page_that_adds_nothing_stops_the_listing():
    fake = FakeRollbar()
    item = fake.add_item("Error")
    page = httpx.Response(200, json={"err": 0, "result": {"items": [item], "page": 1}})
    fake.failing["/api/1/items"] = page
    client, _ = _client(fake)

    with pytest.raises(RollbarAPIError, match="page 2"):
        client.list_items(PROJECT, statuses=ALL)


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(200, text="<html>bad gateway</html>"),
        httpx.Response(200, json={"err": 1, "message": "boom"}),
        httpx.Response(200, json={"err": 0, "result": {"items": "nope"}}),
        httpx.Response(200, json={"err": 0, "result": {"items": [{"title": "no id"}]}}),
    ],
)
def test_an_unexpected_answer_is_an_error_not_an_empty_project(answer):
    fake = FakeRollbar()
    fake.failing["/api/1/items"] = answer
    client, _ = _client(fake)

    with pytest.raises(RollbarAPIError):
        client.list_items(PROJECT, statuses=ALL)


def test_server_and_network_errors_are_retried_with_backoff():
    fake = FakeRollbar()
    fake.add_item("Error")
    fake.queued["/api/1/items"] = [http_error(502), httpx.ConnectError("reset")]
    client, sleeps = _client(fake)

    assert len(client.list_items(PROJECT, statuses=ALL)) == 1
    assert sleeps == [1, 2]


def test_a_server_error_that_persists_is_raised():
    fake = FakeRollbar()
    fake.failing["/api/1/items"] = http_error(503)
    client, sleeps = _client(fake)

    with pytest.raises(RollbarAPIError, match="HTTP 503"):
        client.list_items(PROJECT, statuses=ALL)
    assert len(sleeps) == 3


def test_a_network_error_that_persists_names_no_secret():
    fake = FakeRollbar()
    fake.failing["/api/1/items"] = httpx.ConnectError(f"cannot connect with {TOKEN}")
    client, _ = _client(fake)

    with pytest.raises(RollbarAPIError) as raised:
        client.list_items(PROJECT, statuses=ALL)
    assert TOKEN not in str(raised.value)


@pytest.mark.parametrize(
    ("status", "message"),
    [(401, "invalid access token"), (403, "insufficient privileges: read scope is required")],
)
def test_a_refused_token_raises_an_auth_error_with_rollbars_reason(status, message):
    fake = FakeRollbar()
    fake.failing["/api/1/items"] = http_error(status, message)
    client, _ = _client(fake)

    with pytest.raises(RollbarAuthError, match=message):
        client.list_items(PROJECT, statuses=ALL)


def test_a_wrong_token_is_refused():
    client, _ = _client(FakeRollbar(), token="someone-elses-token")

    with pytest.raises(RollbarAuthError, match="invalid access token"):
        client.list_items(PROJECT, statuses=ALL)


def test_a_rate_limit_is_raised_without_sleeping():
    fake = FakeRollbar()
    fake.failing["/api/1/items"] = http_error(429, "rate limit")
    client, sleeps = _client(fake)

    with pytest.raises(RollbarRateLimitedError):
        client.list_items(PROJECT, statuses=ALL)
    assert sleeps == []


def test_a_deleted_occurrence_reads_as_none():
    fake = FakeRollbar()
    item = fake.add_item("Error")
    client, _ = _client(fake)
    occurrence_id = item["activating_occurrence_id"]

    assert client.occurrence(occurrence_id)["id"] == occurrence_id
    del fake.occurrences[occurrence_id]
    assert client.occurrence(occurrence_id) is None


def test_an_occurrence_without_data_is_an_error():
    fake = FakeRollbar()
    fake.failing["/api/1/instance/"] = httpx.Response(200, json={"err": 0, "result": {"id": 1}})
    client, _ = _client(fake)

    with pytest.raises(RollbarAPIError):
        client.occurrence(1)
