"""Tests for deletion reconciliation and tombstone emission (forget-on-delete)."""

from fake_knock import FakeKnockClient, make_sample_knock_data

from cognee_community_connector_knock.knock import KnockWalker


def test_tombstone_emitted_when_workflow_deleted():
    data = make_sample_knock_data()
    client = FakeKnockClient(sample_data=data)
    walker = KnockWalker(client=client, include=("workflows",))

    state = {}
    docs1 = list(walker.walk(state))
    assert len(docs1) == 2
    assert all(not d["_deleted"] for d in docs1)

    # Now remove one workflow from mock client data
    client.data["workflows"] = [client.data["workflows"][0]]  # only keep user-welcome

    docs2 = list(walker.walk(state))
    # Should only emit the tombstone for the deleted workflow
    assert len(docs2) == 1
    tombstone = docs2[0]
    assert tombstone["id"] == "workflow:password-reset"
    assert tombstone["_deleted"] is True


def test_tombstone_emitted_when_channel_deleted():
    data = make_sample_knock_data()
    client = FakeKnockClient(sample_data=data)
    walker = KnockWalker(client=client, include=("channels",))

    state = {}
    docs1 = list(walker.walk(state))
    assert len(docs1) == 2

    # Remove the second channel
    client.data["channels"] = [client.data["channels"][0]]

    docs2 = list(walker.walk(state))
    assert len(docs2) == 1
    tombstone = docs2[0]
    assert tombstone["id"] == "channel:slack-community"
    assert tombstone["_deleted"] is True


def test_no_spurious_tombstones_when_data_unchanged():
    data = make_sample_knock_data()
    client = FakeKnockClient(sample_data=data)
    walker = KnockWalker(client=client, include=("workflows",))

    state = {}
    list(walker.walk(state))

    # Second run without changes
    docs2 = list(walker.walk(state))
    assert len(docs2) == 0
