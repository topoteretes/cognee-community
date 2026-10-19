"""MailchimpClient: key parsing, auth errors, retries and paging."""

import httpx
import pytest

from cognee_community_connector_mailchimp import mailchimp as mc_module
from cognee_community_connector_mailchimp.mailchimp import (
    MailchimpAuthError,
    MailchimpClient,
    MailchimpError,
    MailchimpForbiddenError,
    MailchimpNotFoundError,
    MailchimpRateLimitError,
    data_centre,
)

KEY = "0123456789abcdef" * 2 + "-us22"  # built, so no key-shaped literal


def _client(fake, key=None):
    return MailchimpClient(
        key or fake.api_key, http_client=fake.http_client(), sleep=lambda s: None
    )


def _scripted(*responses):
    """A client whose transport answers with the given responses in order."""
    queue = list(responses)
    seen = []

    def handler(request):
        seen.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    sleeps = []
    client = MailchimpClient(
        KEY, http_client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=sleeps.append
    )
    return client, seen, sleeps


def test_data_centre_comes_from_the_key():
    assert data_centre(KEY) == "us22"
    assert MailchimpClient(KEY).base_url == "https://us22.api.mailchimp.com/3.0"
    for bad in ("not-a-key", "0123456789abcdef0123456789abcdef", "xyz-us22", KEY + "/evil"):
        with pytest.raises(ValueError, match="Mailchimp API key"):
            data_centre(bad)


def test_bad_key_is_an_auth_error_without_the_key(fake):
    wrong = "fedcba9876543210" * 2 + "-us22"
    with pytest.raises(MailchimpAuthError) as err:
        _client(fake, key=wrong).get_account()
    assert "API Key Invalid" in str(err.value)
    assert wrong not in str(err.value)


def test_key_from_another_data_centre_is_an_auth_error(fake):
    # Live: a us22 key on the us1 host gives 403 "...linked to a different datacenter".
    client = _client(fake)
    client.base_url = "https://us1.api.mailchimp.com/3.0"
    with pytest.raises(MailchimpAuthError, match="different datacenter"):
        client.get_account()


def test_other_403_and_404_are_not_retried():
    client, seen, _ = _scripted(
        httpx.Response(403, json={"title": "Forbidden", "detail": "Not allowed."}),
        httpx.Response(404, json={"title": "Resource Not Found", "detail": "Gone."}),
    )
    with pytest.raises(MailchimpForbiddenError):
        client.request("campaigns")
    with pytest.raises(MailchimpNotFoundError):
        client.request("campaigns/x/content")
    assert len(seen) == 2


def test_429_waits_for_retry_after_then_succeeds():
    client, seen, sleeps = _scripted(
        httpx.Response(429, headers={"retry-after": "3"}),
        httpx.Response(200, json={"ok": True}),
    )
    assert client.request("campaigns") == {"ok": True}
    assert sleeps == [3.0]
    assert len(seen) == 2


def test_long_retry_after_stops_instead_of_sleeping():
    # cognee holds its dlt staging lock while a source runs.
    client, seen, sleeps = _scripted(httpx.Response(429, headers={"retry-after": "3600"}))
    with pytest.raises(MailchimpRateLimitError, match="asked to wait 3600s"):
        client.request("campaigns")
    assert sleeps == []
    assert len(seen) == 1


def test_429_that_never_clears_is_a_rate_limit_error():
    client, _, sleeps = _scripted(*[httpx.Response(429, headers={"retry-after": "1"})] * 5)
    with pytest.raises(MailchimpRateLimitError, match="after 5 tries"):
        client.request("campaigns")
    assert sleeps == [1.0] * 4


def test_5xx_and_network_errors_back_off_then_give_up():
    client, seen, sleeps = _scripted(
        httpx.ConnectError("down"),
        httpx.Response(502),
        httpx.Response(503),
        httpx.Response(500),
        httpx.Response(500),
    )
    with pytest.raises(MailchimpError, match="500"):
        client.request("campaigns")
    assert len(seen) == 5
    assert sleeps == [2.0, 4.0, 8.0, 16.0]


def test_odd_200_bodies_stop_instead_of_reading_as_empty():
    # An empty or partial page must never look like a complete, empty listing.
    for body in (
        httpx.Response(200, text="<html>maintenance</html>"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"campaigns": []}),
        httpx.Response(200, json={"campaigns": None, "total_items": 0}),
    ):
        client, _, _ = _scripted(body)
        with pytest.raises(MailchimpError, match="unexpected"):
            list(client.list_campaigns())


def test_a_listing_that_moves_while_paging_says_so(monkeypatch):
    monkeypatch.setattr(mc_module, "PAGE_COUNT", 2)
    page = {"campaigns": [{"id": "a"}, {"id": "b"}], "total_items": 4}
    # Total changed between pages: one campaign was deleted mid-read.
    client, _, _ = _scripted(
        httpx.Response(200, json=page),
        httpx.Response(200, json={"campaigns": [{"id": "d"}], "total_items": 3}),
    )
    listing = client.list_campaigns()
    assert [c["id"] for c in listing] == ["a", "b", "d"]
    assert listing.moved
    # An empty page while the total says there is more.
    client, _, _ = _scripted(
        httpx.Response(200, json=page),
        httpx.Response(200, json={"campaigns": [], "total_items": 4}),
    )
    listing = client.list_campaigns()
    assert len(list(listing)) == 2
    assert listing.moved
    # A steady listing does not.
    client, _, _ = _scripted(
        httpx.Response(200, json=page),
        httpx.Response(200, json={"campaigns": [{"id": "c"}, {"id": "d"}], "total_items": 4}),
    )
    listing = client.list_campaigns()
    assert len(list(listing)) == 4
    assert not listing.moved


def test_errors_name_the_endpoint_not_the_record():
    # A contact's id is the MD5 of its email address.
    member = "7de8517bce4457e8390aa4006a1880fb"
    client, _, _ = _scripted(httpx.Response(404, json={"title": "Resource Not Found"}))
    with pytest.raises(MailchimpNotFoundError) as err:
        list(client.list_notes("aaaaaaaaaa", member))
    assert "lists/{id}/members/{id}/notes" in str(err.value)
    assert member not in str(err.value)
    assert "aaaaaaaaaa" not in str(err.value)


def test_campaigns_are_paged_oldest_first(fake, monkeypatch):
    monkeypatch.setattr(mc_module, "PAGE_COUNT", 2)
    for i in range(5):
        fake.add_campaign(f"Campaign {i}", "Body.")
    titles = [c["settings"]["title"] for c in _client(fake).list_campaigns()]
    assert titles == [f"Campaign {i}" for i in range(5)]
    pages = [r for r in fake.requests if r.url.path.endswith("/campaigns")]
    assert [int(r.url.params["offset"]) for r in pages] == [0, 2, 4]
    assert all(r.url.params["sort_dir"] == "ASC" for r in pages)


def test_requests_only_ask_for_the_fields_they_use(seeded):
    client = _client(seeded)
    list(client.list_campaigns())
    request = seeded.requests[-1]
    assert "campaigns.settings.title" in request.url.params["fields"]
    assert "total_items" in request.url.params["fields"]
    # Contacts are never asked for merge fields (address, phone, birthday), and
    # for their email address only when a note should carry it.
    list(client.list_members(seeded.list_id))
    assert "merge_fields" not in seeded.requests[-1].url.params["fields"]
    assert "email_address" not in seeded.requests[-1].url.params["fields"]
    list(client.list_members(seeded.list_id, with_email=True))
    assert "members.email_address" in seeded.requests[-1].url.params["fields"]
