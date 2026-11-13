"""The Webflow client: auth, retries, rate limits and complete listings."""

import httpx
import pytest
from fake_webflow import TOKEN, FakeWebflow

from cognee_community_connector_webflow import WebflowAPIError, WebflowClient


@pytest.fixture
def fake():
    fake = FakeWebflow()
    site = fake.add_site()
    fake.collection_id = fake.add_collection(site, "posts")
    return fake


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def client(fake, sleeps):
    return WebflowClient(TOKEN, transport=fake.transport(), sleep=sleeps.append)


def test_token_is_required(monkeypatch):
    monkeypatch.delenv("WEBFLOW_API_TOKEN", raising=False)
    with pytest.raises(ValueError, match="WEBFLOW_API_TOKEN"):
        WebflowClient()


def test_token_falls_back_to_the_environment(fake, monkeypatch):
    monkeypatch.setenv("WEBFLOW_API_TOKEN", TOKEN)
    assert WebflowClient(transport=fake.transport()).get("/sites")["sites"]


def test_a_bad_token_raises_with_the_api_message(fake):
    client = WebflowClient("wrong", transport=fake.transport())
    with pytest.raises(WebflowAPIError) as error:
        client.get("/sites")
    assert error.value.status == 401
    assert error.value.code == "not_authorized"
    assert "Request not authorized" in str(error.value)


def test_the_token_is_never_part_of_an_error_message(fake):
    client = WebflowClient("secret-value", transport=fake.transport())
    with pytest.raises(WebflowAPIError) as error:
        client.get("/sites")
    assert "secret-value" not in str(error.value)


def test_a_missing_resource_is_not_retried(client, fake, sleeps):
    with pytest.raises(WebflowAPIError) as error:
        client.get("/collections/nope")
    assert error.value.status == 404
    assert sleeps == []
    assert len(fake.requests_to("/collections/nope")) == 1


def test_rate_limit_waits_for_retry_after_then_succeeds(client, fake, sleeps):
    fake.fail("/sites", 429, times=2, code="too_many_requests", headers={"Retry-After": "7"})

    assert client.get("/sites")["sites"]

    assert sleeps == [7.0, 7.0]


def test_server_errors_back_off_exponentially(client, fake, sleeps):
    fake.fail("/sites", 503, times=3)

    assert client.get("/sites")["sites"]

    assert sleeps == [1.0, 2.0, 4.0]


def test_a_persistent_server_error_raises_after_the_retry_budget(client, fake, sleeps):
    fake.fail("/sites", 500, times=10)

    with pytest.raises(WebflowAPIError) as error:
        client.get("/sites")

    assert error.value.status == 500
    assert len(sleeps) == 4  # five attempts, four waits


def test_network_errors_are_retried(sleeps):
    attempts = []

    def flaky(request):
        attempts.append(request)
        if len(attempts) < 3:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json={"sites": []})

    client = WebflowClient(TOKEN, transport=httpx.MockTransport(flaky), sleep=sleeps.append)

    assert client.get("/sites") == {"sites": []}
    assert len(attempts) == 3


def test_paginate_walks_every_page(client, fake):
    for number in range(250):
        fake.add_item(fake.collection_id, f"Post {number}")

    rows = list(client.paginate(f"/collections/{fake.collection_id}/items", "items"))

    assert len(rows) == 250
    calls = fake.requests_to("/items")
    assert [call[1]["offset"] for call in calls] == ["0", "100", "200"]
    assert {call[1]["limit"] for call in calls} == {"100"}


def test_paginate_raises_when_the_listing_is_cut_short(sleeps):
    def cut_short(_request):
        body = {"items": [], "pagination": {"limit": 100, "offset": 0, "total": 3}}
        return httpx.Response(200, json=body)

    client = WebflowClient(TOKEN, transport=httpx.MockTransport(cut_short), sleep=sleeps.append)

    with pytest.raises(WebflowAPIError, match="incomplete"):
        list(client.paginate("/collections/x/items", "items"))


def test_list_all_reads_again_when_the_listing_shifted(client, fake):
    for number in range(250):
        fake.add_item(fake.collection_id, f"Post {number}")
    first = fake.items[fake.collection_id][0]["id"]
    shifted = []

    def delete_an_early_item_while_paging(path, params):
        # A row ahead of the read position is deleted after page one was read:
        # everything shifts up and one row is never seen.
        if path.endswith("/items") and params.get("offset") == "100" and not shifted:
            shifted.append(True)
            fake.delete_item(fake.collection_id, first)

    fake.on_request = delete_an_early_item_while_paging

    rows = client.list_all(f"/collections/{fake.collection_id}/items", "items")

    assert len(rows) == 249
    assert len({row["id"] for row in rows}) == 249
    assert first not in {row["id"] for row in rows}


def test_list_all_gives_up_on_a_listing_that_never_settles(client, fake):
    for number in range(250):
        fake.add_item(fake.collection_id, f"Post {number}")

    def keep_deleting(path, params):
        if path.endswith("/items") and params.get("offset") == "100":
            fake.delete_item(fake.collection_id, fake.items[fake.collection_id][0]["id"])

    fake.on_request = keep_deleting

    with pytest.raises(WebflowAPIError, match="kept changing"):
        client.list_all(f"/collections/{fake.collection_id}/items", "items")


def test_a_token_pasted_with_a_trailing_newline_still_works(fake):
    client = WebflowClient(f"{TOKEN}\n", transport=fake.transport())

    assert client.get("/sites")["sites"]


def test_a_token_with_spaces_inside_is_refused_without_echoing_it():
    with pytest.raises(ValueError, match="spaces or control characters") as error:
        WebflowClient("abc def-SECRET")

    assert "SECRET" not in str(error.value)


def test_a_token_with_control_characters_is_refused():
    with pytest.raises(ValueError, match="control characters"):
        WebflowClient("abc\x00def")


def test_a_retry_after_that_is_too_long_stops_the_run_instead_of_sleeping(client, fake, sleeps):
    fake.fail("/sites", 429, times=10, code="too_many_requests", headers={"Retry-After": "3600"})

    with pytest.raises(WebflowAPIError, match="too long") as error:
        client.get("/sites")

    assert error.value.status == 429
    assert sleeps == []


@pytest.mark.parametrize("content", [b"", b"<html>maintenance</html>", b"[]", b'"ok"'])
def test_a_200_that_is_not_a_json_object_is_an_error_not_an_empty_listing(content, sleeps):
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=content))
    client = WebflowClient(TOKEN, transport=transport, sleep=sleeps.append)

    with pytest.raises(WebflowAPIError, match="did not return a JSON object"):
        client.get("/sites")


def test_a_listing_that_never_ends_is_stopped(sleeps):
    rows = [{"id": str(number)} for number in range(100)]
    # No pagination block and a full page every time, as a server that ignores offset would.
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={"items": rows}))
    client = WebflowClient(TOKEN, transport=transport, sleep=sleeps.append)

    with pytest.raises(WebflowAPIError, match="did not end"):
        list(client.paginate("/collections/x/items", "items"))


def test_check_active_is_asked_before_every_request_and_can_stop_the_run(client, fake):
    for number in range(250):
        fake.add_item(fake.collection_id, f"Post {number}")
    asked = []

    def revoked_after_two_requests():
        asked.append(True)
        if len(asked) > 2:
            raise PermissionError("access revoked")

    client.check_active = revoked_after_two_requests

    with pytest.raises(PermissionError):
        list(client.paginate(f"/collections/{fake.collection_id}/items", "items"))

    assert len(fake.requests_to("/items")) == 2, "the third request must never be sent"
