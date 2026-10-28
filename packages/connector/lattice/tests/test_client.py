"""The Lattice client: auth, regions, pagination in both list shapes, retries, errors."""

import httpx
import pytest
from fake_lattice import API_KEY, FakeLattice

from cognee_community_connector_lattice import LatticeAPIError, LatticeClient
from cognee_community_connector_lattice.client import PAGE_SIZE, region_url


def make_client(fake: FakeLattice, sleeps: list | None = None, **kwargs) -> LatticeClient:
    sleeps = sleeps if sleeps is not None else []
    return LatticeClient(API_KEY, transport=fake.transport(), sleep=sleeps.append, **kwargs)


def test_an_api_key_is_required(monkeypatch):
    monkeypatch.delenv("LATTICE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="LATTICE_API_KEY"):
        LatticeClient()


def test_the_key_can_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("LATTICE_API_KEY", API_KEY)
    fake = FakeLattice()
    fake.add_user("Ada Lovelace")
    client = LatticeClient(transport=fake.transport())
    assert [u["name"] for u in client.list_all("/v1/users")] == ["Ada Lovelace"]


def test_a_wrong_key_names_the_key_and_region_as_suspects():
    fake = FakeLattice()
    client = LatticeClient("wrong", transport=fake.transport())
    with pytest.raises(LatticeAPIError, match="401") as caught:
        client.get("/v1/users")
    assert "check the API key and the region" in str(caught.value)
    assert "wrong" not in str(caught.value)


@pytest.mark.parametrize(
    ("region", "host"),
    [
        (None, "api.latticehq.com"),
        ("us", "api.latticehq.com"),
        ("EMEA", "api.emea.latticehq.com"),
    ],
)
def test_the_region_picks_the_base_url(region, host):
    fake = FakeLattice()
    make_client(fake, region=region).get("/v1/users")
    assert fake.hosts == [host]


def test_the_region_can_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("LATTICE_REGION", "emea")
    assert region_url() == "https://api.emea.latticehq.com"


def test_an_unknown_region_is_refused():
    with pytest.raises(ValueError, match="Unknown Lattice region 'apac'"):
        region_url("apac")


def test_a_listing_is_read_across_pages_without_gaps_or_repeats():
    fake = FakeLattice()
    ids = [fake.add_user(f"User {i:03d}") for i in range(250)]
    rows = make_client(fake).list_all("/v1/users")

    assert sorted(r["id"] for r in rows) == sorted(ids)
    assert len(fake.requests_to("/v1/users")) == 3
    assert {call[2]["limit"] for call in fake.requests_to("/v1/users")} == {str(PAGE_SIZE)}


def test_the_goal_updates_page_info_shape_is_followed_too():
    fake = FakeLattice()
    goal = fake.add_goal("Ship it", [fake.add_user("Ada")])
    for i in range(130):
        fake.add_goal_update(goal, f"check-in {i}")
    rows = make_client(fake).list_all(f"/v1/goals/{goal}/updates")

    assert len(rows) == 130
    assert len({r["id"] for r in rows}) == 130
    assert len(fake.requests_to("/updates")) == 2


def test_an_empty_listing_is_empty():
    assert make_client(FakeLattice()).list_all("/v1/goals") == []


def test_a_rate_limit_is_waited_out_with_retry_after_and_retried():
    fake = FakeLattice()
    fake.add_user("Ada")
    fake.fail("/v1/users", 429, times=2, headers={"Retry-After": "39"})
    sleeps: list[float] = []

    assert len(make_client(fake, sleeps).list_all("/v1/users")) == 1
    assert sleeps == [39.0, 39.0]


def test_a_server_error_is_retried_with_backoff():
    fake = FakeLattice()
    fake.add_user("Ada")
    fake.fail("/v1/users", 503, times=2)
    sleeps: list[float] = []

    assert len(make_client(fake, sleeps).list_all("/v1/users")) == 1
    assert sleeps == [1.0, 2.0]


def test_a_rate_limit_that_never_clears_raises_instead_of_returning_less():
    fake = FakeLattice()
    fake.add_user("Ada")
    fake.fail("/v1/users", 429, times=10, headers={"Retry-After": "1"})
    with pytest.raises(LatticeAPIError, match="429"):
        make_client(fake).list_all("/v1/users")


def test_a_failure_on_a_later_page_raises_instead_of_returning_the_first_pages():
    fake = FakeLattice()
    for i in range(150):
        fake.add_user(f"User {i}")
    client = make_client(fake)
    calls = []

    def second_page_fails(path, params):
        calls.append(params)
        if "startingAfter" in params:
            fake.fail("/v1/users", 500, times=10)

    fake.on_request = second_page_fails
    with pytest.raises(LatticeAPIError, match="500"):
        client.list_all("/v1/users")


def test_a_network_error_is_retried_then_raised():
    def refuse(request):
        raise httpx.ConnectError("boom", request=request)

    sleeps: list[float] = []
    client = LatticeClient(API_KEY, transport=httpx.MockTransport(refuse), sleep=sleeps.append)
    with pytest.raises(httpx.ConnectError):
        client.get("/v1/users")
    assert len(sleeps) == 4  # five attempts


def test_more_rows_without_a_cursor_is_an_incomplete_listing_not_the_end():
    def broken(request):
        return httpx.Response(
            200,
            json={"object": "list", "hasMore": True, "endingCursor": None, "data": [{"id": "a"}]},
        )

    client = LatticeClient(API_KEY, transport=httpx.MockTransport(broken))
    with pytest.raises(LatticeAPIError, match="incomplete"):
        client.list_all("/v1/users")


def test_a_cursor_that_repeats_cannot_loop_forever():
    def stuck(request):
        return httpx.Response(
            200, json={"object": "list", "hasMore": True, "endingCursor": "same", "data": [{}]}
        )

    client = LatticeClient(API_KEY, transport=httpx.MockTransport(stuck))
    with pytest.raises(LatticeAPIError, match="incomplete"):
        client.list_all("/v1/users")


def test_an_error_body_without_json_still_names_the_status():
    client = LatticeClient(
        API_KEY, transport=httpx.MockTransport(lambda r: httpx.Response(404, text="nope"))
    )
    with pytest.raises(LatticeAPIError) as caught:
        client.get("/v1/goals/x")
    assert caught.value.status == 404
