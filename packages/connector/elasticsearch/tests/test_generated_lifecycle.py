"""Compare hundreds of generated lifecycle sequences with an independent oracle."""

import json
from copy import deepcopy

import pytest
from conftest import FakeElasticsearch, document
from hypothesis import given, settings
from hypothesis import strategies as st
from test_elasticsearch import sync

operation = st.tuples(
    st.sampled_from(["upsert", "upsert", "delete"]),
    st.sampled_from(["a", "b"]),
    st.sampled_from(["1", "x:y", "é/🚀"]),
    st.integers(min_value=-1000, max_value=1000),
    st.text(alphabet='abc \n汉é🚀"\\', max_size=20),
    st.booleans(),
)


@settings(max_examples=200, deadline=None, derandomize=True, database=None)
@given(
    operations=st.lists(operation, min_size=1, max_size=25),
    page_size=st.integers(min_value=1, max_value=5),
)
def test_generated_sync_matches_selected_upstream_after_every_change(operations, page_size):
    upstream = {}
    materialized = {}
    state = {}
    client = FakeElasticsearch()
    previous = {}
    for step, (action, index, id, date, body, published) in enumerate(operations):
        if action == "delete":
            upstream.pop((index, id), None)
        else:
            upstream[index, id] = document(
                id, index=index, date=date, seq=step + 1, body=body, published=published
            )
        client.documents = list(reversed(upstream.values()))
        # Fail a request before successful replay, including on deletion runs.
        if step % 7 == 0:
            before = deepcopy(state)
            client.fail_at = len(client.requests) + 1
            with pytest.raises(ConnectionError):
                sync(client, state, page_size=page_size, query={"term": {"published": True}})
            assert state == before
            client.fail_at = None
        rows = sync(client, state, page_size=page_size, query={"term": {"published": True}})
        expected = {
            key: {"title": "Guide", "body": item["source"]["body"]}
            for key, item in upstream.items()
            if item["source"]["published"]
        }
        expected_changed = {key for key, value in expected.items() if previous.get(key) != value}
        expected_deleted = previous.keys() - expected.keys()
        changed, deleted = set(), set()
        for row in rows:
            key = tuple(json.loads(row["id"].removeprefix("test-scope:")))
            if row["deleted"]:
                deleted.add(key)
                materialized.pop(key)
            else:
                changed.add(key)
                materialized[key] = json.loads(row["content"])
        assert changed == expected_changed
        assert deleted == expected_deleted
        assert materialized == expected
        assert sync(client, state, page_size=page_size, query={"term": {"published": True}}) == []
        previous = expected
    # A completed empty snapshot must clear the last document and all hashes.
    client.documents = []
    rows = sync(client, state, page_size=page_size, query={"term": {"published": True}})
    assert len(rows) == len(materialized) and all(row["deleted"] for row in rows)
    assert state["revisions"] == state["fingerprints"] == {}
