"""BasecampClient: headers, Link paging, retries, 404 handling, 304 and token refresh."""

import httpx
import pytest

from cognee_community_connector_basecamp.basecamp import (
    BasecampAccountInactiveError,
    BasecampAuthError,
    BasecampClient,
    BasecampError,
    BasecampNotFoundError,
    BasecampRateLimitError,
)

TOKEN = "secret-token-value"
UA = "cognee-basecamp-tests (test@example.com)"


def _client(handler, **kwargs) -> BasecampClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return BasecampClient("999", TOKEN, UA, http_client=http, sleep=lambda s: None, **kwargs)


def test_sends_user_agent_and_bearer_token(fake):
    client = BasecampClient("999", TOKEN, UA, http_client=fake.http_client())
    list(client.list_recordings("Message", status="active"))
    request = fake.requests[0]
    assert request.headers["user-agent"] == UA
    assert request.headers["authorization"] == f"Bearer {TOKEN}"


def test_follows_link_header_across_pages(fake):
    for i in range(5):
        fake.add("Message", f"m{i}")
    client = BasecampClient("999", TOKEN, UA, http_client=fake.http_client())
    titles = [r["title"] for r in client.list_recordings("Message", status="active")]
    assert titles == ["m4", "m3", "m2", "m1", "m0"]  # newest first, all 3 pages
    assert len(fake.requests) == 3


def test_etag_returns_nothing_when_unchanged(fake):
    fake.add("Message", "hello")
    client = BasecampClient("999", TOKEN, UA, http_client=fake.http_client())
    list(client.list_recordings("Message", status="active"))
    etag = client.last_etag
    assert etag

    assert list(client.list_recordings("Message", status="active", etag=etag)) == []
    assert client.last_etag == etag

    fake.add("Message", "new one")
    assert [r["title"] for r in client.list_recordings("Message", status="active", etag=etag)] == [
        "new one",
        "hello",
    ]


def test_retries_429_using_retry_after():
    calls = []
    waits = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200, json=[])

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = BasecampClient("999", TOKEN, UA, http_client=http, sleep=waits.append)
    assert client.request("https://3.basecampapi.com/999/x.json").status_code == 200
    assert len(calls) == 2
    assert waits == [3.0]


def test_retries_5xx_then_gives_up():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    with pytest.raises(BasecampError, match="gave up"):
        _client(handler).request("https://3.basecampapi.com/999/x.json")
    assert len(calls) == 5


def test_404_is_not_retried():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(404, json={"status": 404, "error": "Not Found"})

    with pytest.raises(BasecampNotFoundError):
        _client(handler).request("https://3.basecampapi.com/999/x.json")
    assert len(calls) == 1


def test_inactive_account_raises_clear_error():
    def handler(request):
        return httpx.Response(404, headers={"Reason": "Account Inactive"})

    with pytest.raises(BasecampAccountInactiveError, match="inactive"):
        _client(handler).request("https://3.basecampapi.com/999/x.json")


def test_401_without_refresh_token_raises_auth_error():
    def handler(request):
        return httpx.Response(401)

    with pytest.raises(BasecampAuthError):
        _client(handler).request("https://3.basecampapi.com/999/x.json")


def test_401_refreshes_token_once_and_retries():
    seen_tokens = []

    def handler(request):
        if request.url.host == "launchpad.37signals.com":
            assert b"grant_type=refresh_token" in request.content
            return httpx.Response(200, json={"access_token": "fresh-token"})
        seen_tokens.append(request.headers["authorization"])
        if request.headers["authorization"] == f"Bearer {TOKEN}":
            return httpx.Response(401)
        return httpx.Response(200, json=[])

    client = _client(handler, refresh_token="r", client_id="cid", client_secret="cs")
    assert client.request("https://3.basecampapi.com/999/x.json").status_code == 200
    assert seen_tokens == [f"Bearer {TOKEN}", "Bearer fresh-token"]


def test_token_never_appears_in_errors():
    def handler(request):
        return httpx.Response(503)

    with pytest.raises(BasecampError) as excinfo:
        _client(handler).request(f"https://3.basecampapi.com/999/x.json?token={TOKEN}")
    assert TOKEN not in str(excinfo.value)


def _scripted(*responses):
    """A client whose transport answers with the given responses in order."""
    queue = list(responses)
    seen, waits = [], []

    def handler(request):
        seen.append(request)
        return queue.pop(0)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    return BasecampClient("999", TOKEN, UA, http_client=http, sleep=waits.append), seen, waits


URL = "https://3.basecampapi.com/999/x.json"


def test_long_retry_after_stops_instead_of_sleeping():
    # cognee holds its dlt staging lock while a source runs.
    for value in ("3600", "inf", "1e9"):
        client, seen, waits = _scripted(httpx.Response(429, headers={"Retry-After": value}))
        with pytest.raises(BasecampRateLimitError, match="asked to wait"):
            client.request(URL)
        assert (waits, len(seen)) == ([], 1), value


def test_unusable_retry_after_falls_back_to_backoff():
    # float("nan") parses, and max(0.0, nan) is 0.0 or nan depending on the
    # order; 0 or a negative value would retry back to back.
    for value in ("nan", "-inf", "-5", "0"):
        client, _, waits = _scripted(
            httpx.Response(429, headers={"Retry-After": value}),
            httpx.Response(429, headers={"Retry-After": value}),
            httpx.Response(200, json=[]),
        )
        assert client.request(URL).status_code == 200
        assert waits == [2.0, 4.0], value


def test_429_that_never_clears_is_a_rate_limit_error():
    client, _, _ = _scripted(*[httpx.Response(429, headers={"Retry-After": "1"})] * 5)
    with pytest.raises(BasecampRateLimitError, match="gave up"):
        client.request(URL)


def test_long_wait_on_a_5xx_is_a_rate_limit_too():
    client, _, waits = _scripted(httpx.Response(503, headers={"Retry-After": "600"}))
    with pytest.raises(BasecampRateLimitError, match="asked to wait 600s"):
        client.request(URL)
    assert waits == []


def test_an_odd_200_body_never_reads_as_an_empty_listing():
    # An empty listing makes the sweep forget everything, so anything that is
    # not a JSON list stops the run instead.
    for response in (
        httpx.Response(200, json={}),
        httpx.Response(200, json={"error": "x"}),
        httpx.Response(200, content=b""),
        httpx.Response(200, content=b"null"),
    ):
        client, _, _ = _scripted(response)
        with pytest.raises(BasecampError, match="unexpected body"):
            list(client.iter_pages("/projects/recordings.json"))


def test_paging_that_does_not_advance_stops_instead_of_looping():
    calls = []

    def handler(request):
        calls.append(request)
        # A regression here would loop forever; fail fast instead.
        assert len(calls) < 20, "paging looped"
        return httpx.Response(
            200, json=[{"id": 1}], headers={"link": f'<{request.url}>; rel="next"'}
        )

    with pytest.raises(BasecampError, match="did not advance"):
        list(_client(handler).iter_pages("/projects/recordings.json"))
