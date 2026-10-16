"""Opt-in tests on uniquely named, disposable Elasticsearch indices."""

import os
from copy import deepcopy

import dlt
import pytest
from elasticsearch import ApiError, Elasticsearch, helpers

from cognee_community_connector_elasticsearch import elasticsearch_source
from cognee_community_connector_elasticsearch.elasticsearch import _sync

pytestmark = pytest.mark.live


def sync(reader, index, state):
    return _sync(
        reader,
        state,
        index=index,
        query={"term": {"published": True}},
        updated_field="updated_at",
        fields=["title", "body"],
        title_field="title",
        page_size=500,
        keep_alive="2m",
        scope="live",
    )


def test_api_key_deep_pagination_updates_query_departures_and_empty_index(live):
    admin, reader, index, _ = live
    helpers.bulk(
        admin,
        (
            {
                "_index": index,
                "_id": str(i),
                "_source": {
                    "title": f"Guide {i}",
                    "body": f"Knowledge {i}",
                    "updated_at": 1000,
                    "published": True,
                    "unselected_secret": "do not ingest",
                },
            }
            for i in range(10017)
        ),
        refresh="wait_for",
    )
    state = {}
    rows = sync(reader, index, state)
    assert len(rows) == 10017
    assert len({row["id"] for row in rows}) == 10017
    assert all("unselected_secret" not in row["content"] for row in rows)
    assert sync(reader, index, state) == []
    admin.update(index=index, id="0", doc={"body": "Updated at the same date"})
    admin.update(index=index, id="1", doc={"body": "Backdated", "updated_at": 500})
    admin.update(index=index, id="2", doc={"published": False})
    admin.delete(index=index, id="3")
    admin.index(
        index=index,
        id="new-old",
        document={"body": "New old document", "updated_at": 300, "published": True},
    )
    admin.indices.refresh(index=index)
    rows = sync(reader, index, state)
    assert len(rows) == 5
    assert sum(row["deleted"] for row in rows) == 2
    assert sync(reader, index, state) == []
    admin.delete_by_query(index=index, query={"match_all": {}}, refresh=True)
    rows = sync(reader, index, state)
    assert len(rows) == 10016 and all(row["deleted"] for row in rows)
    assert state["revisions"] == {}
    assert sync(reader, index, state) == []


def test_revoked_key_aborts_without_state_change(live):
    admin, reader, index, _ = live
    admin.index(
        index=index,
        id="1",
        document={"body": "Test", "updated_at": 1000, "published": True},
        refresh="wait_for",
    )
    state = {}
    sync(reader, index, state)
    before = deepcopy(state)
    # Invalidate just this fixture's uniquely named key, not other keys.
    key_id = admin.security.get_api_key(name=index)["api_keys"][0]["id"]
    admin.security.invalidate_api_key(ids=[key_id])
    with pytest.raises(ApiError):
        sync(reader, index, state)
    assert state == before


def test_public_factory_auth_and_persisted_merge(live, tmp_path):
    admin, _reader, index, encoded_key = live
    admin.index(
        index=index,
        id="1",
        document={
            "title": "Factory test",
            "body": "Initial",
            "updated_at": 1000,
            "published": True,
        },
        refresh="wait_for",
    )
    pipeline = dlt.pipeline(
        pipeline_name="live_factory",
        pipelines_dir=str(tmp_path / "dlt"),
        destination=dlt.destinations.sqlalchemy(
            credentials=f"sqlite:///{tmp_path / 'staging.sqlite'}"
        ),
        dataset_name="live_factory",
    )

    def source():
        return elasticsearch_source(
            source_id=index,
            index=index,
            url=os.environ["ES_TEST_URL"],
            api_key=encoded_key,
            fields=["title", "body"],
        )

    table = next(iter(source().resources))

    def run():
        pipeline.run(source(), write_disposition="merge")
        with pipeline.sql_client() as sql:
            return sql.execute_sql(f'SELECT content FROM "{table}"')

    assert "Initial" in run()[0][0]
    admin.update(
        index=index, id="1", doc={"body": "Factory update", "updated_at": 2000}, refresh="wait_for"
    )
    assert "Factory update" in run()[0][0]
    assert len(run()) == 1
    assert encoded_key not in str(pipeline.state)
    admin.delete(index=index, id="1", refresh="wait_for")
    assert run() == []


def test_missing_update_date_aborts_real_scan(live):
    admin, reader, index, _ = live
    admin.index(
        index=index,
        id="no-date",
        document={"body": "Missing timestamp", "published": True},
        refresh="wait_for",
    )
    state = {}
    with pytest.raises(ValueError, match="update date"):
        sync(reader, index, state)
    assert state == {}


@pytest.mark.parametrize("kind", ["missing", "no_wildcard_match", "closed"])
def test_unavailable_indices_preserve_successful_state(live, kind):
    admin, reader, index, _ = live
    admin.index(
        index=index,
        id="1",
        document={"body": "Retain me", "updated_at": 1000, "published": True},
        refresh="wait_for",
    )
    state = {}
    sync(reader, index, state)
    before = deepcopy(state)
    target = index
    if kind == "closed":
        admin.indices.close(index=index)
    elif kind == "missing":
        target += "-missing"
    else:
        target += "-missing-*"
    # Administrator access isolates unavailable-index behavior from ACL errors.
    with pytest.raises((ApiError, RuntimeError)):
        sync(admin, target, state)
    assert state == before


def test_api_key_without_index_permission_does_not_forget(live):
    admin, reader, index, _ = live
    admin.index(
        index=index,
        id="1",
        document={"body": "Retain me", "updated_at": 1000, "published": True},
        refresh="wait_for",
    )
    state = {}
    sync(reader, index, state)
    before = deepcopy(state)
    denied_key = admin.security.create_api_key(
        name=index + "-denied",
        role_descriptors={
            "reader": {
                "cluster": [],
                "indices": [
                    {"names": [index + "-other"], "privileges": ["read", "view_index_metadata"]}
                ],
            }
        },
    )
    denied = Elasticsearch(os.environ["ES_TEST_URL"], api_key=denied_key["encoded"])
    try:
        with pytest.raises(ApiError) as failure:
            sync(denied, index, state)
        assert failure.value.status_code == 403
        assert state == before
    finally:
        denied.close()
        admin.security.invalidate_api_key(ids=[denied_key["id"]])


def test_multivalued_update_date_aborts_before_deletion(live):
    admin, reader, index, _ = live
    admin.index(
        index=index,
        id="1",
        document={"body": "Valid", "updated_at": 1000, "published": True},
        refresh="wait_for",
    )
    state = {}
    sync(reader, index, state)
    before = deepcopy(state)
    admin.index(
        index=index,
        id="2",
        document={"body": "Ambiguous", "updated_at": [1000, 2000], "published": True},
        refresh="wait_for",
    )
    with pytest.raises(ValueError, match="one readable update date"):
        sync(reader, index, state)
    assert state == before


@pytest.mark.parametrize("mapping", [{"type": "date", "doc_values": False}, {"type": "keyword"}])
def test_unsupported_update_mapping_is_not_an_empty_snapshot(live, mapping):
    admin, reader, index, _ = live
    admin.indices.put_mapping(index=index, properties={"unsupported_date": mapping})
    admin.index(
        index=index,
        id="1",
        document={
            "body": "Test",
            "updated_at": 1000,
            "unsupported_date": "2026-10-08",
            "published": True,
        },
        refresh="wait_for",
    )
    state = {}
    sync(reader, index, state)
    before = deepcopy(state)
    with pytest.raises((ApiError, ValueError)):
        _sync(
            reader,
            state,
            index=index,
            query={"match_all": {}},
            updated_field="unsupported_date",
            fields=True,
            title_field=None,
            page_size=1,
            keep_alive="2m",
            scope="live",
        )
    assert state == before


def test_mixed_date_nanos_indices_same_ids_and_submillisecond_edits(live):
    admin, _reader, index, _ = live
    nanos_index = index + "-nanos"
    admin.indices.create(
        index=nanos_index, mappings={"properties": {"updated_at": {"type": "date_nanos"}}}
    )
    try:
        admin.index(
            index=index,
            id="same-id",
            document={
                "body": "Milliseconds",
                "updated_at": "2026-10-08T10:00:00.123Z",
                "published": True,
            },
        )
        admin.index(
            index=nanos_index,
            id="same-id",
            document={
                "body": "Nanoseconds",
                "updated_at": "2026-10-08T10:00:00.123456789Z",
                "published": True,
            },
        )
        admin.indices.refresh(index=index + "*")
        state = {}
        rows = sync(admin, index + "*", state)
        assert len(rows) == len({row["id"] for row in rows}) == 2
        assert sync(admin, index + "*", state) == []
        admin.update(
            index=nanos_index,
            id="same-id",
            doc={"body": "Submillisecond edit", "updated_at": "2026-10-08T10:00:00.123456790Z"},
            refresh="wait_for",
        )
        rows = sync(admin, index + "*", state)
        assert len(rows) == 1 and "Submillisecond edit" in rows[0]["content"]
    finally:
        admin.indices.delete(index=nanos_index)


def test_filtered_alias_stays_filtered_inside_the_pit(live):
    admin, _reader, index, _ = live
    alias = index + "-alias"
    admin.indices.put_alias(index=index, name=alias, filter={"term": {"published": True}})
    admin.index(
        index=index,
        id="selected",
        document={"body": "Public", "updated_at": 1000, "published": True},
    )
    admin.index(
        index=index,
        id="excluded",
        document={"body": "Private", "updated_at": 1000, "published": False},
    )
    admin.indices.refresh(index=index)
    state = {}
    rows = _sync(
        admin,
        state,
        index=alias,
        query={"match_all": {}},
        updated_field="updated_at",
        fields=True,
        title_field=None,
        page_size=1,
        keep_alive="2m",
        scope="live",
    )
    assert len(rows) == 1 and "Public" in rows[0]["content"]
    assert "Private" not in str(rows)


def test_expired_pit_stops_the_sync_and_preserves_state(live):
    admin, reader, index, _ = live
    admin.index(
        index=index,
        id="1",
        document={"body": "First", "updated_at": 1000, "published": True},
        refresh="wait_for",
    )
    state = {}
    sync(reader, index, state)
    before = deepcopy(state)
    original = reader.search

    def expired(**kwargs):
        reader.close_point_in_time(id=kwargs["pit"]["id"])
        return original(**kwargs)

    reader.search = expired
    with pytest.raises(ApiError):
        sync(reader, index, state)
    assert state == before


def test_recreated_index_with_reused_revisions_fetches_new_content(live):
    admin, reader, index, _ = live

    def create_document(body):
        admin.index(
            index=index,
            id="same-id",
            document={"body": body, "updated_at": "2026-10-08T10:00:00Z", "published": True},
            refresh="wait_for",
        )

    create_document("Old index incarnation")
    state = {}
    sync(reader, index, state)
    old_revisions = deepcopy(state["revisions"])
    admin.indices.delete(index=index)
    admin.indices.create(
        index=index,
        settings={"number_of_shards": 2, "number_of_replicas": 0},
        mappings={"properties": {"updated_at": {"type": "date"}, "published": {"type": "boolean"}}},
    )
    create_document("New index incarnation")
    rows = sync(reader, index, state)
    assert state["revisions"] == old_revisions  # An actual reused date/sequence/term.
    assert len(rows) == 1 and "New index incarnation" in rows[0]["content"]


def test_disabled_source_is_not_silently_treated_as_deleted(live):
    admin, _reader, index, _ = live
    disabled_index = index + "-disabled-source"
    admin.indices.create(
        index=disabled_index,
        mappings={
            "_source": {"enabled": False},
            "properties": {"updated_at": {"type": "date"}, "published": {"type": "boolean"}},
        },
    )
    try:
        admin.index(
            index=disabled_index,
            id="1",
            document={
                "body": "Unavailable source",
                "updated_at": "2026-10-08T10:00:00Z",
                "published": True,
            },
            refresh="wait_for",
        )
        state = {}
        with pytest.raises((ApiError, ValueError), match="_source"):
            sync(admin, disabled_index, state)
        assert state == {}
    finally:
        admin.indices.delete(index=disabled_index)


def test_read_key_without_metadata_permission_gets_an_explicit_error(live):
    admin, _reader, index, _ = live
    key = admin.security.create_api_key(
        name=index + "-no-metadata",
        role_descriptors={
            "reader": {"cluster": [], "indices": [{"names": [index], "privileges": ["read"]}]}
        },
    )
    client = Elasticsearch(os.environ["ES_TEST_URL"], api_key=key["encoded"])
    try:
        state = {}
        with pytest.raises(ApiError) as failure:
            sync(client, index, state)
        assert failure.value.status_code == 403 and state == {}
    finally:
        client.close()
        admin.security.invalidate_api_key(ids=[key["id"]])
