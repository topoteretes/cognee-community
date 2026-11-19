"""Tests for deletion reconciliation and tombstone emission (forget-on-delete)."""

from fake_resend import FakeResendClient, make_sample_resend_data

from cognee_community_connector_resend.resend import ResendWalker


def test_tombstone_emitted_when_domain_deleted():
    data = make_sample_resend_data()
    client = FakeResendClient(sample_data=data)
    walker = ResendWalker(client=client, include=("domains",))

    state = {}
    docs1 = list(walker.walk(state))
    assert len(docs1) == 2
    assert all(not d["_deleted"] for d in docs1)

    # Now remove one domain from mock client data
    client.data["domains"] = [client.data["domains"][0]]  # only keep dom_prod_001

    docs2 = list(walker.walk(state))
    # Should only emit the tombstone for the deleted domain
    assert len(docs2) == 1
    tombstone = docs2[0]
    assert tombstone["id"] == "domain:dom_staging_002"
    assert tombstone["_deleted"] is True


def test_tombstone_emitted_when_audience_deleted():
    data = make_sample_resend_data()
    client = FakeResendClient(sample_data=data)
    walker = ResendWalker(client=client, include=("audiences",))

    state = {}
    docs1 = list(walker.walk(state))
    assert len(docs1) == 1

    # Remove the audience
    client.data["audiences"] = []

    docs2 = list(walker.walk(state))
    assert len(docs2) == 1
    tombstone = docs2[0]
    assert tombstone["id"] == "audience:aud_saas_users"
    assert tombstone["_deleted"] is True


def test_no_spurious_tombstones_when_data_unchanged():
    data = make_sample_resend_data()
    client = FakeResendClient(sample_data=data)
    walker = ResendWalker(client=client, include=("domains",))

    state = {}
    list(walker.walk(state))

    # Second run without changes
    docs2 = list(walker.walk(state))
    assert len(docs2) == 0
