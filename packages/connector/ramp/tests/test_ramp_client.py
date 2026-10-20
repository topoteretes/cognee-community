"""Client tests: auth, the expired-token 404, rate limits, retries and error messages."""

import httpx
import pytest
from fake_ramp import FakeRamp, ago, client_for, serve, sync

from cognee_community_connector_ramp import RampClient, ramp
from cognee_community_connector_ramp.ramp import RampAuthError, _iter_rows


@pytest.fixture
def fake():
    return FakeRamp()


def test_token_is_reused_and_renewed_once_when_refused(fake):
    fake.add("t1", ago(3), memo="A")
    client = client_for(fake)

    client.get("/developer/v1/transactions")
    client.get("/developer/v1/transactions")
    assert fake.tokens_issued == 1

    fake.valid_token = "revoked-early"
    client.get("/developer/v1/transactions")
    assert fake.tokens_issued == 2


def test_token_response_without_a_token_is_an_auth_error(fake):
    fake.queued = [httpx.Response(200, content=b'{"token_type": "Bearer"}')]
    with pytest.raises(RampAuthError, match="no access token"):
        client_for(fake).get("/developer/v1/transactions")


def test_bad_credentials_raise(fake):
    fake.credentials = ("client", "other-secret")
    with pytest.raises(RampAuthError):
        client_for(fake).get("/developer/v1/transactions")


def test_expired_token_mid_run_never_forgets(fake):
    """A token that can't be renewed mid-run aborts the run; nothing is read as deleted."""
    fake.add("t1", ago(3), memo="Hotel hold", state="PENDING")
    fake.add("t2", ago(2), memo="Taxi")
    state: dict = {}
    client = client_for(fake)
    list(_iter_rows(client, state, {}))

    del fake.transactions["t1"]  # t1 really is gone, so a deletion check will run
    real_token_endpoint = fake._token

    def revoke_and_refuse(request):
        # no new token can be issued, so the deletion check can't confirm anything
        fake.valid_token = "never-matches"
        return httpx.Response(401, content=b'{"error": {"message": "invalid_client"}}')

    fake._token = revoke_and_refuse
    fake.valid_token = "expired"
    with pytest.raises(RampAuthError):
        list(_iter_rows(client, state, {}))
    assert "transaction:t1" in state["rows"], "an auth failure must not read as 'transaction gone'"

    fake._token = real_token_endpoint
    _, deletes, _ = sync(fake, state)
    assert deletes == {"transaction:t1"}


def test_expired_token_during_a_deletion_check_keeps_the_transaction(fake):
    """A token rotated between runs answers 404; it is renewed, not read as a deletion."""
    fake.add("t1", ago(3), memo="Still very much alive")
    state: dict = {}
    client = client_for(fake)
    list(_iter_rows(client, state, {}))

    state["recheck"] = ["t1"]  # a deletion check is due for t1
    fake.valid_token = "rotated-server-side"  # the cached token is no longer accepted
    rows = list(_iter_rows(client, state, {}))

    assert not [row for row in rows if row["_deleted"]]
    assert "transaction:t1" in state["rows"]
    assert fake.tokens_issued == 2


def test_unrecognised_404_is_an_error_not_a_deletion(fake):
    fake.add("t1", ago(3), memo="A")
    state: dict = {}
    sync(fake, state)
    del fake.transactions["t1"]

    # a 404 that doesn't name the record (a proxy, a moved route) deletes nothing
    serve(fake, "/transactions/t1", 404, b"<html>Not Found</html>")
    _, deletes, stats = sync(fake, state)
    assert not deletes
    assert stats["failed"] == 1
    assert "transaction:t1" in state["rows"]


def test_403_on_the_listing_is_fatal(fake):
    fake.add("t1", ago(3), memo="A")
    serve(fake, "/transactions", 403)
    with pytest.raises(ramp.RampAccessError, match="scopes"):
        sync(fake, {})


def test_rate_limit_waits_retry_after_then_cuts_short(fake):
    fake.add("t1", ago(3), memo="A")
    sleeps: list = []
    fake.queued = [httpx.Response(429, headers={"retry-after": "7"}, content=b"{}")]
    upserts, _, _ = sync(fake, {}, sleeps=sleeps)
    assert set(upserts) == {"transaction:t1"}
    assert sleeps == [7.0]

    fake.queued = [httpx.Response(429, headers={"retry-after": "1"}, content=b"{}")] * 5
    upserts, _, stats = sync(fake, {})
    assert not upserts
    assert stats["failed_rate_limit"] == 1


def test_long_retry_after_cuts_the_run_short_instead_of_sleeping(fake):
    fake.add("t1", ago(3), memo="A")
    sleeps: list = []
    fake.queued = [httpx.Response(429, headers={"retry-after": "3600"}, content=b"{}")]
    upserts, _, stats = sync(fake, {}, sleeps=sleeps)
    assert sleeps == [] and not upserts
    assert stats["failed_rate_limit"] == 1


def test_network_errors_are_retried(fake):
    attempts = []

    def flaky(request):
        attempts.append(request)
        if len(attempts) == 1:
            raise httpx.ConnectError("boom")
        return fake._handle(request)

    client = RampClient(
        "client",
        "secret",
        http=httpx.Client(transport=httpx.MockTransport(flaky)),
        sleep=lambda _s: None,
    )
    assert client.get("/developer/v1/transactions") == {"data": [], "page": {"next": None}}


def test_next_page_url_is_never_followed(fake):
    """Only the start token is taken from page.next, so a token can't leak to another host."""
    fake.page_size_cap = 1
    fake.add("t1", ago(3), memo="A")
    fake.add("t2", ago(2), memo="B")
    sync(fake, {})
    paths = {c[1] for c in fake.calls}
    assert "/developer/v1/whatever" not in paths


def test_error_messages_never_carry_record_ids(fake):
    client = client_for(fake)
    with pytest.raises(ramp.RampNotFoundError) as caught:
        client.get("/developer/v1/transactions/7dc9c4c7-9bbf-4606-a855-115c202d3262")
    assert "7dc9c4c7" not in str(caught.value)
    assert "/developer/v1/transactions/{id}" in str(caught.value)


def test_repr_hides_credentials():
    assert "secret" not in repr(RampClient("client", "secret"))
