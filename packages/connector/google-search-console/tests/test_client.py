"""The client: auth, token renewal, retries, encoded property URLs and startRow paging."""

import httpx
import pytest
from fake_search_console import (
    ACCESS_TOKEN,
    CLIENT_ID,
    CLIENT_SECRET,
    REFRESH_TOKEN,
    FakeSearchConsole,
    _error,
)

from cognee_community_connector_google_search_console import (
    SearchConsoleAPIError,
    SearchConsoleClient,
    SearchConsoleCredentials,
)
from cognee_community_connector_google_search_console import client as client_module

SITE = "sc-domain:example.com"
BODY = {"startDate": "2026-09-01", "endDate": "2026-09-30", "dimensions": ["query"]}


@pytest.fixture
def fake(today):
    fake = FakeSearchConsole(today)
    fake.add_site(SITE)
    return fake


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def client(fake, sleeps):
    return SearchConsoleClient(
        SearchConsoleCredentials(access_token=ACCESS_TOKEN),
        transport=fake.transport(),
        sleep=sleeps.append,
    )


def test_list_sites_returns_the_properties_and_permissions(client, fake):
    fake.add_site("https://blog.example.com/", "siteUnverifiedUser")

    assert client.list_sites() == [
        {"siteUrl": SITE, "permissionLevel": "siteOwner"},
        {"siteUrl": "https://blog.example.com/", "permissionLevel": "siteUnverifiedUser"},
    ]


@pytest.mark.parametrize("url", [SITE, "https://example.com/", "https://example.com/blog/"])
def test_property_urls_are_percent_encoded_in_the_path(client, fake, url):
    fake.add_site(url)

    client.query(url, BODY)

    (_method, path, _body) = fake.calls[-1]
    assert path.startswith("/webmasters/v3/sites/")
    assert "/" not in path.removeprefix("/webmasters/v3/sites/").split("/searchAnalytics")[0]


def test_a_bad_token_raises_with_google_s_message(fake):
    client = SearchConsoleClient(SearchConsoleCredentials(access_token="wrong"), transport=fake.transport())
    with pytest.raises(SearchConsoleAPIError) as error:
        client.list_sites()
    assert error.value.status == 401
    assert error.value.reason == "authError"


def test_the_token_is_never_part_of_an_error_message(fake):
    client = SearchConsoleClient(
        SearchConsoleCredentials(access_token="secret-value"), transport=fake.transport()
    )
    with pytest.raises(SearchConsoleAPIError) as error:
        client.list_sites()
    assert "secret-value" not in str(error.value)


def test_an_expired_token_is_renewed_once_and_the_call_retried(fake):
    fake.valid_token = "newer"  # the cached token is stale
    credentials = SearchConsoleCredentials(
        access_token="stale",
        refresh_token=REFRESH_TOKEN,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        transport=fake.transport(),
    )
    client = SearchConsoleClient(credentials, transport=fake.transport(), sleep=lambda _s: None)

    assert client.list_sites()
    assert fake.token_requests == 1


def test_a_token_that_still_fails_after_renewal_is_not_renewed_again(fake):
    credentials = SearchConsoleCredentials(
        access_token="stale",
        refresh_token=REFRESH_TOKEN,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        transport=fake.transport(),
    )
    client = SearchConsoleClient(credentials, transport=fake.transport())
    fake.fail_next("/sites", _error(401, "no", "authError"), _error(401, "no", "authError"))

    with pytest.raises(SearchConsoleAPIError) as error:
        client.list_sites()
    assert error.value.status == 401
    assert fake.token_requests == 1


def test_a_missing_property_is_not_retried(client, fake, sleeps):
    with pytest.raises(SearchConsoleAPIError) as error:
        client.get_site("sc-domain:gone.example")
    assert error.value.status == 404
    assert sleeps == []


def test_a_rate_limit_is_retried_after_its_retry_after(client, fake, sleeps):
    fake.fail_next("/sites", httpx.Response(429, headers={"Retry-After": "7"}))

    assert client.list_sites()
    assert sleeps == [7.0]


def test_a_403_rate_limit_is_retried_but_other_403s_are_not(client, fake, sleeps):
    fake.fail_next("/sites", _error(403, "Rate Limit Exceeded", "rateLimitExceeded"))
    assert client.list_sites()
    assert len(sleeps) == 1

    fake.add_site("https://x.example/", "siteUnverifiedUser")
    with pytest.raises(SearchConsoleAPIError) as error:
        client.query("https://x.example/", BODY)
    assert error.value.status == 403
    assert len(sleeps) == 1


def test_server_errors_back_off_exponentially_then_give_up(client, fake, sleeps):
    fake.fail_next("/sites", *[httpx.Response(503) for _ in range(5)])

    with pytest.raises(SearchConsoleAPIError) as error:
        client.list_sites()
    assert error.value.status == 503
    assert sleeps == [1.0, 2.0, 4.0, 8.0]


def test_a_network_failure_is_retried(fake, sleeps):
    attempts = []

    def flaky(request):
        attempts.append(1)
        if len(attempts) < 3:
            raise httpx.ConnectError("down")
        return fake._handle(request)

    client = SearchConsoleClient(
        SearchConsoleCredentials(access_token=ACCESS_TOKEN),
        transport=httpx.MockTransport(flaky),
        sleep=sleeps.append,
    )

    assert client.list_sites()
    assert len(sleeps) == 2


def test_query_all_walks_start_row_until_a_short_page(client, fake, monkeypatch):
    monkeypatch.setattr(client_module, "ROW_LIMIT", 10)
    for n in range(25):
        fake.add_traffic(SITE, _day(2026, 9, 10), f"q{n:02d}", "/p", clicks=100 - n)

    rows = client.query_all(SITE, BODY, max_rows=1000)

    assert [row["keys"][0] for row in rows] == [f"q{n:02d}" for n in range(25)]
    starts = [body["startRow"] for body in fake.queries_to("searchAnalytics")]
    assert starts == [0, 10, 20]


def test_query_all_stops_at_the_row_cap_and_keeps_the_top_rows(client, fake, monkeypatch):
    monkeypatch.setattr(client_module, "ROW_LIMIT", 10)
    for n in range(25):
        fake.add_traffic(SITE, _day(2026, 9, 10), f"q{n:02d}", "/p", clicks=100 - n)

    rows = client.query_all(SITE, BODY, max_rows=15)

    assert len(rows) == 15
    assert rows[0]["keys"] == ["q00"]
    limits = [body["rowLimit"] for body in fake.queries_to("searchAnalytics")]
    assert limits == [10, 5]


def test_query_all_does_not_ask_again_after_an_exact_last_page(client, fake, monkeypatch):
    monkeypatch.setattr(client_module, "ROW_LIMIT", 10)
    for n in range(10):
        fake.add_traffic(SITE, _day(2026, 9, 10), f"q{n}", "/p")

    assert len(client.query_all(SITE, BODY, max_rows=10)) == 10
    assert len(fake.queries_to("searchAnalytics")) == 1


def test_the_real_row_limit_is_googles_maximum():
    assert client_module.ROW_LIMIT == 25_000


def _day(year, month, day):
    from datetime import date

    return date(year, month, day)


def test_a_long_retry_after_stops_the_run_instead_of_sleeping(client, fake, sleeps):
    fake.fail_next("/sites", httpx.Response(429, headers={"Retry-After": "600"}))

    with pytest.raises(SearchConsoleAPIError) as error:
        client.list_sites()
    assert error.value.is_rate_limit
    assert sleeps == [], "nothing is slept: the staging lock would be held for ten minutes"


def test_waiting_is_capped_for_the_whole_client(client, fake, sleeps):
    for _ in range(6):
        fake.fail_next("/sites", httpx.Response(429, headers={"Retry-After": "59"}))

    with pytest.raises(SearchConsoleAPIError) as error:
        for _ in range(6):
            client.list_sites()
    assert error.value.is_rate_limit
    assert sum(sleeps) <= 300


def test_a_throttling_403_counts_as_a_rate_limit_but_a_permission_403_does_not(client, fake):
    fake.fail_next("/sites", *[_error(403, "slow down", "rateLimitExceeded")] * 5)
    with pytest.raises(SearchConsoleAPIError) as limited:
        client.list_sites()
    assert limited.value.is_rate_limit

    fake.add_site("https://x.example/", "siteUnverifiedUser")
    with pytest.raises(SearchConsoleAPIError) as denied:
        client.query("https://x.example/", BODY)
    assert not denied.value.is_rate_limit


@pytest.mark.parametrize("body", [b"<html>proxy login</html>", b"[]", b'"ok"'])
def test_a_200_that_is_not_a_json_object_is_an_error_not_an_empty_result(body):
    client = SearchConsoleClient(
        SearchConsoleCredentials(access_token=ACCESS_TOKEN),
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=body)),
    )
    with pytest.raises(SearchConsoleAPIError, match="JSON object"):
        client.query(SITE, BODY)
