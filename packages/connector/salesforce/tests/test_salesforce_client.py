"""Client tests: tokens, re-minting, paging, errors and what they may never contain."""

import httpx
import pytest
from fake_salesforce import FakeSalesforce, client_for, json_body

from cognee_community_connector_salesforce import SalesforceClient
from cognee_community_connector_salesforce.salesforce import (
    SalesforceAPIError,
    SalesforceAuthError,
    SalesforceReplicationWindowError,
    SalesforceSourceError,
    SalesforceUnavailableError,
)


@pytest.fixture
def fake():
    return FakeSalesforce()


def test_the_token_is_minted_with_client_credentials_and_names_the_org(fake):
    client = client_for(fake)

    client.connect()

    method, path, _ = fake.calls[0]
    assert (method, path) == ("POST", "/services/oauth2/token")
    assert client.org_id == fake.org_id
    assert client.instance_url == fake.instance


def test_a_wrong_secret_is_an_auth_error_that_never_shows_the_secret(fake):
    client = SalesforceClient(fake.login, fake.client_id, "SUPERSECRET-not-it", http=fake.client())

    with pytest.raises(SalesforceAuthError) as refused:
        client.connect()

    message = str(refused.value)
    assert "invalid_client" in message
    assert "SUPERSECRET" not in message and fake.client_id not in message


def test_a_token_that_names_no_org_is_refused(fake):
    """Sync state is keyed on the org id, read from the token's identity URL."""
    client = client_for(fake)
    fake.queued = [httpx.Response(200, json={"access_token": "t", "instance_url": fake.instance})]

    with pytest.raises(SalesforceAuthError, match="names no org"):
        client.connect()


def test_feed_tracking_is_read_from_the_object_list(fake):
    fake.feed_enabled = {"Case"}

    assert client_for(fake).feed_enabled() == {"Case"}


def test_an_expired_token_is_reminted_once_and_the_request_retried(fake):
    """Client-credentials tokens carry no expires_in (recorded), so an expiry shows up as
    401 INVALID_SESSION_ID in the middle of a run."""
    fake.add("Account", Name="Alphacorp")
    client = client_for(fake)
    client.connect()
    fake.expire_token()

    records, _ = client.query_page("SELECT Id FROM Account")

    assert len(records) == 1
    assert [c[1] for c in fake.calls].count("/services/oauth2/token") == 2


def test_a_token_refused_twice_is_an_auth_error(fake):
    client = client_for(fake)
    client.connect()
    refused = httpx.Response(
        401, json=[{"message": "Session expired or invalid", "errorCode": "INVALID_SESSION_ID"}]
    )

    def always_refuse(request):
        if request.url.path == "/services/oauth2/token":
            return fake._token(request)
        return refused

    fake._handle = always_refuse
    with pytest.raises(SalesforceAuthError, match="fresh token"):
        client.query_page("SELECT Id FROM Account")


def test_paging_follows_next_records_url_to_the_end(fake):
    fake.page_size = 2
    for n in range(5):
        fake.add("Account", Name=f"A{n}")
    client = client_for(fake)

    records, next_url = client.query_page("SELECT Id FROM Account")
    seen = len(records)
    while next_url:
        records, next_url = client.query_page(next_url=next_url)
        seen += len(records)

    assert seen == 5


@pytest.mark.parametrize(
    "body, message",
    [
        ({"done": True}, "records list"),
        ({"records": [], "totalSize": 0}, "done flag"),
        ({"records": [], "done": False}, "without a next page"),
        ({"records": ["x"], "done": True}, "non-object"),
    ],
)
def test_a_malformed_page_raises_instead_of_reading_as_empty(fake, body, message):
    """A page that read as "no records" would look like every stored record was deleted."""
    client = client_for(fake)
    client.connect()
    fake.queued = [httpx.Response(200, json=body)]

    with pytest.raises(SalesforceAPIError, match=message):
        client.query_page("SELECT Id FROM Account")


def test_5xx_is_retried_then_raised(fake):
    sleeps: list[float] = []
    client = client_for(fake, sleeps)
    client.connect()
    fake.queued = [httpx.Response(503, content=b"")] * 5

    with pytest.raises(SalesforceUnavailableError):
        client.query_page("SELECT Id FROM Account")

    assert sleeps == [1, 2]


def test_a_not_found_only_ever_means_not_there(fake):
    """Recorded: a deleted record and an id that never existed get the same 404."""
    alive = fake.add("Account", Name="Alphacorp")
    gone = fake.add("Account", Name="Bravocorp")
    fake.delete("Account", gone)
    client = client_for(fake)

    assert client.record_exists("Account", alive) is True
    assert client.record_exists("Account", gone) is False
    assert client.record_exists("Account", "001g500000zzzzzAAA") is False


def test_a_404_with_another_error_code_is_not_a_delete(fake):
    client = client_for(fake)
    client.connect()
    fake.queued = [
        httpx.Response(404, json=[{"errorCode": "FUNCTIONALITY_NOT_ENABLED", "message": "no"}])
    ]

    with pytest.raises(SalesforceAPIError, match="FUNCTIONALITY_NOT_ENABLED"):
        client.record_exists("Account", "001g500000mtBX5AAM")


@pytest.mark.parametrize(
    "message",
    ["startDate before org replication enabled date", "startDate cannot be more than 30 days ago"],
)
def test_both_replication_window_refusals_are_one_error(fake, message):
    client = client_for(fake)
    client.connect()
    fake.queued = [
        httpx.Response(400, json=[{"message": message, "errorCode": "INVALID_REPLICATION_DATE"}])
    ]

    with pytest.raises(SalesforceReplicationWindowError):
        client.get_deleted("Account", "2026-10-29T19:00:00Z", "2026-10-29T20:00:00Z")


def test_the_org_api_usage_is_read_from_the_header(fake):
    client = client_for(fake)
    client.query_page("SELECT Id FROM Account")

    assert client.api_usage is not None and client.api_usage[1] == 15000


@pytest.mark.parametrize(
    "login",
    ["http://acme.my.salesforce.com", "acme.my.salesforce.com", "https://login.salesforce.com"],
)
def test_the_login_url_must_be_an_https_my_domain(login):
    with pytest.raises(ValueError):
        SalesforceClient(login, "id", "secret")


@pytest.mark.parametrize("status", [400, 401, 403, 404, 500, 503])
def test_credentials_never_reach_a_message_or_a_log(fake, caplog, capsys, status):
    """Nothing else pins this; Asana leaked a token through an httpx header error."""
    secret = "SUPERSECRET-salesforce-value"
    fake.client_secret = secret
    client = SalesforceClient(
        fake.login, fake.client_id, secret, http=fake.client(), sleep=lambda _s: None
    )
    client.connect()
    token = fake.valid_token
    fake.queued = [httpx.Response(status, json=[{"message": "nope", "errorCode": "SOMETHING"}])] * 6

    with caplog.at_level("DEBUG"):
        try:
            client.query_page("SELECT Id FROM Account")
        except SalesforceSourceError as exc:  # every error this client raises
            haystack = " ".join([str(exc), repr(exc), repr(getattr(exc, "__dict__", {}))])
        else:
            haystack = ""
    streams = capsys.readouterr()
    haystack += " ".join([caplog.text, streams.out, streams.err])

    assert secret not in haystack
    assert token not in haystack
    assert fake.client_id not in haystack


def test_the_fake_answers_like_the_recordings(fake):
    """Pins the fake to the recorded shapes it is built from."""
    client = client_for(fake)
    client.connect()
    response = fake._record("Account", "001g500000zzzzzAAA")
    assert response.status_code == 404
    assert json_body(response) == [
        {"errorCode": "NOT_FOUND", "message": "The requested resource does not exist"}
    ]
