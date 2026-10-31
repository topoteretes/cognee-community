"""Auth and HTTP client tests against ``FakeDocuSign`` (no DocuSign account needed)."""

import time

import pytest
from fake_docusign import (
    ACCOUNT_ID,
    API_HOST,
    API_ROOT,
    INTEGRATION_KEY,
    OAUTH_HOST,
    USER_ID,
    FakeDocuSign,
)

from cognee_community_connector_docusign.auth import (
    DocuSignAuth,
    DocuSignAuthError,
    resolve_account,
    resolve_credentials,
)
from cognee_community_connector_docusign.client import (
    DocuSignAPIError,
    DocuSignClient,
    DocuSignRateLimitError,
)

LIST = {"from_date": "2000-01-01T00:00:00Z", "from_to_status": "changed"}


@pytest.fixture
def fake(rsa_keys):
    return FakeDocuSign(public_key=rsa_keys[1])


def _jwt_credentials(rsa_keys):
    return resolve_credentials(INTEGRATION_KEY, USER_ID, rsa_keys[0])


def _client(fake, rsa_keys, sleeps=None, clock=None):
    session = fake.session()
    auth = DocuSignAuth(_jwt_credentials(rsa_keys), session)
    return DocuSignClient(
        auth,
        f"https://{API_HOST}",
        ACCOUNT_ID,
        session,
        sleep=(sleeps.append if sleeps is not None else lambda _s: None),
        **({"clock": clock} if clock else {}),
    )


# -- credentials -------------------------------------------------------------


def test_credentials_fall_back_to_env_and_are_required(monkeypatch, rsa_keys):
    for name in (
        "DOCUSIGN_ACCESS_TOKEN",
        "DOCUSIGN_INTEGRATION_KEY",
        "DOCUSIGN_USER_ID",
        "DOCUSIGN_PRIVATE_KEY",
        "DOCUSIGN_PRIVATE_KEY_PATH",
        "DOCUSIGN_ENVIRONMENT",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="DOCUSIGN_USER_ID"):
        resolve_credentials("key")

    # A key exported through a one-line env var carries literal backslash-n.
    monkeypatch.setenv("DOCUSIGN_INTEGRATION_KEY", "key")
    monkeypatch.setenv("DOCUSIGN_USER_ID", "guid")
    monkeypatch.setenv("DOCUSIGN_PRIVATE_KEY", rsa_keys[0].strip().replace("\n", "\\n"))
    credentials = resolve_credentials()
    assert credentials.private_key == rsa_keys[0].strip()
    assert credentials.environment == "demo"
    assert credentials.oauth_host == "account-d.docusign.com"
    assert "BEGIN" not in repr(credentials)
    assert "tok" not in repr(resolve_credentials(access_token="tok-secret"))


def test_private_key_can_be_read_from_a_file(tmp_path, rsa_keys):
    path = tmp_path / "private.pem"
    path.write_text(rsa_keys[0])
    credentials = resolve_credentials("key", "guid", private_key_path=str(path))
    assert credentials.private_key == rsa_keys[0].strip()


def test_access_token_wins_and_production_uses_the_production_host():
    credentials = resolve_credentials(
        "key", "guid", "pem", access_token="tok", environment="Production"
    )
    assert credentials.access_token == "tok"
    assert credentials.oauth_host == "account.docusign.com"
    with pytest.raises(ValueError, match="environment"):
        resolve_credentials(access_token="tok", environment="staging")


# -- JWT grant ---------------------------------------------------------------


def test_jwt_token_is_cached_and_renewed_before_it_expires(fake, rsa_keys):
    now = [time.time()]  # the fake verifies exp against the real clock
    session = fake.session()
    auth = DocuSignAuth(_jwt_credentials(rsa_keys), session, clock=lambda: now[0])

    first = auth.token()
    assert auth.token() == first
    assert fake.token_requests == 1

    now[0] += 3600 - 200  # inside the 5 minute renewal margin
    assert auth.token() != first
    assert fake.token_requests == 2


def test_rejected_token_is_renewed_once_and_the_request_retried(fake, rsa_keys):
    fake.add_envelope("A", "text")
    client = _client(fake, rsa_keys)
    assert client.get_json("envelopes", LIST)["envelopes"]

    fake.valid_tokens.clear()  # e.g. revoked server-side
    assert client.get_json("envelopes", LIST)["envelopes"]
    assert fake.token_requests == 2


def test_static_token_cannot_be_renewed(fake):
    fake.static_token = "static"
    client = DocuSignClient(
        DocuSignAuth(resolve_credentials(access_token="static"), fake.session()),
        f"https://{API_HOST}",
        ACCOUNT_ID,
        fake.session(),
    )
    assert client.get_json("envelopes", LIST) == {
        "resultSetSize": "0",
        "totalSetSize": "0",
        "startPosition": "0",
        "endPosition": "0",
    }
    fake.static_token = "other"
    with pytest.raises(DocuSignAuthError, match="JWT grant"):
        client.get_json("envelopes", LIST)


def test_missing_consent_explains_how_to_grant_it(fake, rsa_keys):
    fake.consent_required = True
    auth = DocuSignAuth(_jwt_credentials(rsa_keys), fake.session())
    with pytest.raises(DocuSignAuthError, match="consent_required") as raised:
        auth.token()
    assert OAUTH_HOST in str(raised.value)
    assert "oauth/auth" in str(raised.value)


def test_a_bad_private_key_fails_without_echoing_it(fake):
    credentials = resolve_credentials(INTEGRATION_KEY, USER_ID, "not a pem SECRET-MARKER")
    with pytest.raises(DocuSignAuthError) as raised:
        DocuSignAuth(credentials, fake.session()).token()
    assert "SECRET-MARKER" not in str(raised.value)
    assert "SECRET-MARKER" not in repr(raised.value.__cause__)


# -- account discovery -------------------------------------------------------


def test_account_and_base_uri_come_from_userinfo(fake, rsa_keys):
    fake.accounts = [
        {"account_id": "other", "is_default": "false", "base_uri": "https://eu.docusign.net"},
        {"account_id": "main", "is_default": "true", "base_uri": "https://na3.docusign.net/"},
    ]
    session = fake.session()
    auth = DocuSignAuth(_jwt_credentials(rsa_keys), session)

    assert resolve_account(session, auth, OAUTH_HOST) == ("main", "https://na3.docusign.net")
    assert resolve_account(session, auth, OAUTH_HOST, "other") == (
        "other",
        "https://eu.docusign.net",
    )
    with pytest.raises(DocuSignAuthError, match="not available"):
        resolve_account(session, auth, OAUTH_HOST, "missing")


# -- client ------------------------------------------------------------------


def test_pagination_follows_next_uri_across_pages(fake, rsa_keys):
    for number in range(250):
        fake.add_envelope(f"Agreement {number}", "text")
    client = _client(fake, rsa_keys)

    found = list(client.paginate_envelopes({**LIST, "count": 100}))

    assert len(found) == 250
    assert len({e["envelopeId"] for e in found}) == 250
    assert len(fake.listing_queries()) == 3


def test_next_uri_never_sends_the_token_to_another_host(fake, rsa_keys):
    client = _client(fake, rsa_keys)
    url = client._url("https://evil.example.com/restapi/v2.1/accounts/x/envelopes?start_position=5")
    assert url == f"https://{API_HOST}/restapi/v2.1/accounts/x/envelopes?start_position=5"
    assert client._url("envelopes") == f"https://{API_HOST}{API_ROOT}/envelopes"


def test_short_rate_limit_is_waited_out(fake, rsa_keys):
    fake.add_envelope("A", "text")
    sleeps: list[float] = []
    client = _client(fake, rsa_keys, sleeps)
    fake.fail_once("/envelopes", 429, {"Retry-After": "3"})

    assert client.get_json("envelopes", LIST)["envelopes"]
    assert sleeps == [3.0]


def test_long_rate_limit_aborts_instead_of_sleeping(fake, rsa_keys):
    sleeps: list[float] = []
    client = _client(fake, rsa_keys, sleeps, clock=lambda: 1000.0)
    fake.fail_once("/envelopes", 429, {"X-RateLimit-Reset": "4600"})  # an hour away

    with pytest.raises(DocuSignRateLimitError, match="rate limit"):
        client.get_json("envelopes", LIST)
    assert sleeps == []


def test_server_errors_are_retried_then_raised(fake, rsa_keys):
    fake.add_envelope("A", "text")
    sleeps: list[float] = []
    client = _client(fake, rsa_keys, sleeps)

    fake.fail_once("/envelopes", 503)
    assert client.get_json("envelopes", LIST)["envelopes"]
    assert sleeps == [1.0]

    for _ in range(5):
        fake.fail_once("/envelopes", 503)
    with pytest.raises(DocuSignAPIError, match="HTTP 503"):
        client.get_json("envelopes", LIST)


def test_client_errors_carry_status_and_code_but_no_token(fake, rsa_keys):
    client = _client(fake, rsa_keys)
    with pytest.raises(DocuSignAPIError, match="MISSING_FROM_DATE") as raised:
        client.get_json("envelopes", {})
    assert raised.value.status == 400
    assert "token-" not in str(raised.value)
    with pytest.raises(DocuSignAPIError) as missing:
        client.get_bytes("envelopes/nope/documents/combined", {"certificate": "false"})
    assert missing.value.status == 404
