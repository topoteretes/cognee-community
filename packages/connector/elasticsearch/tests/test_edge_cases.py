"""Fault injection, pagination boundaries and lifecycle regression tests."""

import json
from copy import deepcopy
from unittest.mock import Mock

import dlt
import pytest
from conftest import FakeElasticsearch, document
from test_elasticsearch import sync

from cognee_community_connector_elasticsearch import elasticsearch_source
from cognee_community_connector_elasticsearch.elasticsearch import _row


@pytest.mark.parametrize(
    "count,page_size",
    [(0, 1), (1, 1), (2, 1), (2, 2), (3, 2), (4, 2), (5, 2), (17, 3), (17, 10000)],
)
def test_pagination_boundaries(count, page_size):
    client = FakeElasticsearch(document(str(i), date=i // 3 - 2) for i in range(count))
    state = {}
    rows = sync(client, state, page_size=page_size)
    assert len(rows) == count
    assert len({row["id"] for row in rows}) == count
    assert len(state["revisions"]) == count
    client.requests.clear()
    assert sync(client, state, page_size=page_size) == []
    assert all(request["source"] is False for request in client.requests)
    assert len(client.closed) == 2


@pytest.mark.parametrize("failure_at", range(1, 11))
def test_interruption_at_every_inventory_and_delta_request(failure_at):
    client = FakeElasticsearch([document(str(i)) for i in range(6)])
    state = {}
    sync(client, state)
    before = deepcopy(state)
    client.documents = [document(str(i), seq=2, date=2000, body="Edited") for i in range(1, 7)]
    client.requests.clear()
    client.fail_at = failure_at
    with pytest.raises(ConnectionError):
        sync(client, state)
    assert state == before
    assert len(client.closed) == 2
    client.fail_at = None
    rows = sync(client, state)
    assert len(rows) == 7 and sum(row["deleted"] for row in rows) == 1
    assert sync(client, state) == []


def corrupt(response, defect):
    if defect == "timeout":
        response["timed_out"] = True
    elif defect == "terminated":
        response["terminated_early"] = True
    elif defect == "failed_shard":
        response["_shards"]["failed"] = 1
    elif defect == "incomplete_shards":
        response["_shards"]["successful"] = 1
    elif defect == "missing_shards":
        response.pop("_shards")
    elif defect == "missing_hits":
        response.pop("hits")
    elif defect == "non_list_hits":
        response["hits"]["hits"] = {}
    elif defect == "missing_date":
        response["hits"]["hits"][0]["fields"] = {}
    elif defect == "multiple_dates":
        response["hits"]["hits"][0]["fields"]["updated_at"] = [1000, 2000]
    elif defect == "missing_sort":
        response["hits"]["hits"][0].pop("sort")
    elif defect == "short_sort":
        response["hits"]["hits"][0]["sort"] = [1000]
    elif defect == "bad_date_sort":
        response["hits"]["hits"][0]["sort"][0] = "invalid"
    elif defect == "bad_tie_breaker":
        response["hits"]["hits"][0]["sort"][1] = "invalid"
    elif defect == "missing_revision":
        response["hits"]["hits"][0].pop("_seq_no")
    elif defect == "missing_source":
        response["hits"]["hits"][0].pop("_source", None)
    elif defect == "unexpected_delta_id":
        response["hits"]["hits"][0]["_id"] = "outside-the-query"
    elif defect == "duplicate":
        response["hits"]["hits"].append(deepcopy(response["hits"]["hits"][0]))
    elif defect == "null_identity":
        response["hits"]["hits"][0]["_id"] = None
    elif defect == "fractional_revision":
        response["hits"]["hits"][0]["_seq_no"] = 1.25
    elif defect == "boolean_revision":
        response["hits"]["hits"][0]["_seq_no"] = True
    elif defect == "negative_term":
        response["hits"]["hits"][0]["_primary_term"] = -1
    return response


DEFECTS = [
    "timeout",
    "terminated",
    "failed_shard",
    "incomplete_shards",
    "missing_shards",
    "missing_hits",
    "non_list_hits",
    "missing_date",
    "multiple_dates",
    "missing_sort",
    "short_sort",
    "bad_date_sort",
    "bad_tie_breaker",
    "missing_revision",
    "duplicate",
    "null_identity",
    "fractional_revision",
    "boolean_revision",
    "negative_term",
]


@pytest.mark.parametrize("phase", ["inventory", "delta"])
@pytest.mark.parametrize("defect", DEFECTS)
def test_corrupt_response_never_commits_or_drives_deletion(phase, defect):
    client = FakeElasticsearch([document("old"), document("survivor")])
    state = {}
    sync(client, state)
    before = deepcopy(state)
    client.documents = [document("survivor", seq=2, body="Changed")]
    search = client.search
    injected = False

    def faulty(**kwargs):
        nonlocal injected
        response = search(**kwargs)
        matching = (kwargs["source"] is False) == (phase == "inventory")
        if matching and not injected:
            injected = True
            corrupt(response, defect)
        return response

    client.search = faulty
    with pytest.raises((RuntimeError, ValueError, KeyError)):
        sync(client, state)
    assert state == before
    assert len(client.closed) == 2


@pytest.mark.parametrize("defect", ["missing_source", "unexpected_delta_id"])
def test_invalid_delta_never_commits(defect):
    client = FakeElasticsearch([document()])
    state = {}
    search = client.search

    def faulty(**kwargs):
        response = search(**kwargs)
        if kwargs["source"] is not False and response["hits"]["hits"]:
            corrupt(response, defect)
        return response

    client.search = faulty
    with pytest.raises((RuntimeError, ValueError, KeyError)):
        sync(client, state)
    assert state == {}
    assert client.closed


@pytest.mark.parametrize("phase", ["inventory", "delta"])
def test_repeated_page_fails_instead_of_looping(phase):
    client = FakeElasticsearch([document("1"), document("2")])
    state = {}
    search = client.search
    repeated = None

    def faulty(**kwargs):
        nonlocal repeated
        if (kwargs["source"] is False) == (phase == "inventory"):
            if repeated is not None and "search_after" in kwargs:
                return deepcopy(repeated)
            repeated = search(**kwargs)
            return repeated
        return search(**kwargs)

    client.search = faulty
    with pytest.raises(RuntimeError):
        sync(client, state, page_size=1)
    assert state == {}
    assert client.closed


def test_missing_index_or_failed_pit_open_preserves_state():
    client = FakeElasticsearch([document()])
    state = {}
    sync(client, state)
    before = deepcopy(state)
    client.open_point_in_time = Mock(side_effect=PermissionError("Read permission denied"))
    with pytest.raises(PermissionError):
        sync(client, state)
    assert state == before
    assert len(client.closed) == 1


def test_partial_pit_open_is_closed_and_never_drives_deletions():
    client = FakeElasticsearch()
    opened = client.open_point_in_time

    def partial(**kwargs):
        response = opened(**kwargs)
        response["_shards"]["failed"] = 1
        return response

    client.open_point_in_time = partial
    state = {}
    with pytest.raises(RuntimeError):
        sync(client, state)
    assert state == {} and client.closed


@pytest.mark.parametrize("mode", ["raises", "unsuccessful"])
def test_pit_close_failure_does_not_publish_state(mode):
    client = FakeElasticsearch([document()])
    state = {}
    client.close_point_in_time = (
        Mock(side_effect=ConnectionError("Close failed"))
        if mode == "raises"
        else Mock(return_value={"succeeded": False})
    )
    with pytest.raises((ConnectionError, RuntimeError)):
        sync(client, state)
    assert state == {}


def test_revision_changes_and_deleted_then_restored_document():
    client = FakeElasticsearch([document(date=0, body="First")])
    state = {}
    sync(client, state)
    client.documents = []
    assert sync(client, state)[0]["deleted"]
    client.documents = [document(date=-1, body="Restored", seq=2)]
    rows = sync(client, state)
    assert len(rows) == 1 and "Restored" in rows[0]["content"]
    assert state["watermark"] == 0
    assert sync(client, state) == []


def test_upstream_edits_between_pages_wait_until_the_next_pit():
    client = FakeElasticsearch([document("1"), document("2")])
    search = client.search

    def mutate(**kwargs):
        response = search(**kwargs)
        if len(client.requests) == 1:
            client.documents = [document("2", seq=2, body="Next snapshot"), document("3")]
        return response

    client.search = mutate
    state = {}
    first = sync(client, state, page_size=1)
    assert len(first) == 2 and all("Next snapshot" not in row["content"] for row in first)
    second = sync(client, state, page_size=1)
    assert len(second) == 3 and sum(row["deleted"] for row in second) == 1
    assert sync(client, state) == []


@pytest.mark.parametrize("id", ["é /?&", 'quote"slash\\', "x:y", "汉字🚀", "00001"])
def test_unicode_and_special_ids_and_content_are_stable(id):
    item = document(id, body='日本語 café\n🚀\tquoted "text"')
    item["source"]["metadata"] = {"tags": ["a", None, False, 0], "nested": {"key": "值"}}
    client = FakeElasticsearch([item])
    state = {}
    row = sync(client, state, fields=True)[0]
    assert json.loads(row["content"]) == item["source"]
    assert sync(client, state, fields=True) == []


@pytest.mark.parametrize(
    "value,expected", [(None, ""), (0, "0"), (False, "False"), ("", ""), ("Título", "Título")]
)
def test_title_preserves_false_and_zero(value, expected):
    row = _row(
        {"_index": "a", "_id": "1", "_source": {"meta": {"title": value}}},
        scope="scope",
        title_field="meta.title",
    )
    assert row["title"] == expected


def test_scope_is_stable_for_query_key_order_field_order_and_key_rotation():
    def source(query, fields, **kwargs):
        return elasticsearch_source(
            source_id="scope",
            index="articles",
            client=FakeElasticsearch(),
            query=query,
            fields=fields,
            **kwargs,
        )

    first = source({"term": {"published": True}, "boost": 1}, ["body", "title"])
    second = source(
        {"boost": 1, "term": {"published": True}},
        ["title", "body", "body"],
        api_key="rotated-key",
        page_size=1,
        keep_alive="1m",
    )
    assert first.name == second.name


@pytest.mark.parametrize(
    "option,value",
    [
        ("source_id", "another"),
        ("index", "another"),
        ("url", "https://another.example"),
        ("updated_field", "modified_at"),
        ("title_field", "subject"),
        ("title_field", None),
    ],
)
def test_identity_settings_isolate_sources(option, value):
    kwargs = {"source_id": "scope", "index": "articles", "client": FakeElasticsearch()}
    first = elasticsearch_source(**kwargs)
    kwargs[option] = value
    assert elasticsearch_source(**kwargs).name != first.name


@pytest.mark.parametrize(
    "option,value",
    [
        ("title_field", 42),
        ("title_field", []),
        ("updated_field", " "),
        ("keep_alive", " "),
        ("fields", [" "]),
        ("fields", [42]),
        ("source_id", None),
        ("index", False),
        ("page_size", 1.5),
        ("query", []),
    ],
)
def test_invalid_settings_fail_early(option, value):
    kwargs = {"source_id": "scope", "index": "articles", "client": FakeElasticsearch()}
    kwargs[option] = value
    with pytest.raises(ValueError):
        elasticsearch_source(**kwargs)


@pytest.mark.parametrize("disposition", ["replace", "append"])
def test_unsafe_write_modes_refused_without_touching_staging(tmp_path, disposition):
    client = FakeElasticsearch([document()])

    def source():
        return elasticsearch_source(source_id="scope", index="articles", client=client)

    pipeline = dlt.pipeline(
        pipeline_name="safe_modes",
        pipelines_dir=str(tmp_path / "dlt"),
        dataset_name="docs",
        destination=dlt.destinations.sqlalchemy(
            credentials=f"sqlite:///{tmp_path / 'staging.sqlite'}"
        ),
    )
    pipeline.run(source())
    before = deepcopy(pipeline.state["sources"])
    client.requests.clear()
    with pytest.raises(Exception, match="requires write_disposition"):
        pipeline.run(source(), write_disposition=disposition)
    assert pipeline.state["sources"] == before and not client.requests
    table = next(iter(source().resources))
    with pipeline.sql_client() as sql:
        assert len(sql.execute_sql(f'SELECT id FROM "{table}"')) == 1


@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.parametrize("failure", [False, True])
def test_client_lifetime_env_auth_ca_and_no_secret_in_state(monkeypatch, owned, failure):
    client = FakeElasticsearch([document()])
    client.close = Mock()
    if failure:
        client.fail_at = 1
    constructor = Mock(return_value=client)
    monkeypatch.setattr("elasticsearch.Elasticsearch", constructor)
    monkeypatch.setenv("ELASTICSEARCH_URL", "https://cluster.example")
    monkeypatch.setenv("ELASTICSEARCH_API_KEY", "encoded-test-key")
    source = elasticsearch_source(
        source_id="scope",
        index="articles",
        ca_certs="/test/ca.pem",
        client=None if owned else client,
    )
    if failure:
        with pytest.raises(Exception, match="simulated connection failure"):
            list(source)
    else:
        assert len(list(source)) == 1
    assert client.close.call_count == int(owned)
    if owned:
        assert constructor.call_args.kwargs["api_key"] == "encoded-test-key"
        assert constructor.call_args.kwargs["ca_certs"] == "/test/ca.pem"
        assert constructor.call_args.kwargs["retry_on_timeout"] is True
    else:
        constructor.assert_not_called()


def test_pending_load_recovers_without_losing_the_extracted_delta(tmp_path, monkeypatch):
    client = FakeElasticsearch([document(body="Old staged content")])

    def source():
        return elasticsearch_source(source_id="scope", index="articles", client=client)

    pipeline = dlt.pipeline(
        pipeline_name="load_recovery",
        pipelines_dir=str(tmp_path / "dlt"),
        dataset_name="docs",
        destination=dlt.destinations.sqlalchemy(
            credentials=f"sqlite:///{tmp_path / 'staging.sqlite'}"
        ),
    )
    pipeline.run(source())
    table = next(iter(source().resources))
    client.documents = [document(seq=2, body="Recovered load")]
    pipeline.extract(source())
    pipeline.normalize()
    assert pipeline.has_pending_data
    actual_load = pipeline._load
    monkeypatch.setattr(pipeline, "_load", Mock(side_effect=ConnectionError("destination offline")))
    with pytest.raises(ConnectionError, match="destination offline"):
        pipeline.load()
    with pipeline.sql_client() as sql:
        assert "Old staged content" in sql.execute_sql(f'SELECT content FROM "{table}"')[0][0]
    monkeypatch.setattr(pipeline, "_load", actual_load)
    request_count = len(client.requests)
    pipeline.run(source())  # dlt resumes its pending package before new extraction.
    assert len(client.requests) == request_count
    with pipeline.sql_client() as sql:
        assert "Recovered load" in sql.execute_sql(f'SELECT content FROM "{table}"')[0][0]
    assert not pipeline.has_pending_data


def test_caller_mutating_query_or_fields_cannot_change_a_created_source():
    query = {"term": {"published": True}}
    fields = ["body"]
    client = FakeElasticsearch([document("1"), document("2", published=False)])
    source = elasticsearch_source(
        source_id="scope", index="articles", client=client, query=query, fields=fields
    )
    query["term"]["published"] = False
    fields.append("secret")
    rows = list(source)
    assert len(rows) == 1 and rows[0]["id"].endswith('["articles","1"]')
    assert "secret" not in rows[0]["content"]


@pytest.mark.parametrize("kind", ["old_version", "missing_scope_hook"])
def test_unsupported_cognee_refused_before_extraction(monkeypatch, kind):
    from cognee.tasks.ingestion import dlt_utils

    if kind == "old_version":
        monkeypatch.setattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0)
    else:
        monkeypatch.delattr(dlt_utils, "PIPELINE_SCOPE_ATTR")
    client = FakeElasticsearch()
    with pytest.raises(RuntimeError, match="scoped document sync"):
        elasticsearch_source(source_id="scope", index="articles", client=client)
    assert not client.requests and not client.closed


def test_primary_term_change_fetches_content_without_timestamp_change():
    client = FakeElasticsearch([document()])
    state = {}
    sync(client, state)
    client.documents[0]["source"]["body"] = "New primary term"
    original = client.search

    def search(**kwargs):
        result = original(**kwargs)
        for hit in result["hits"]["hits"]:
            hit["_primary_term"] = 2
        return result

    client.search = search
    rows = sync(client, state)
    assert len(rows) == 1 and "New primary term" in rows[0]["content"]
    assert sync(client, state) == []


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"articles": {"settings": {}}},
        {"articles": {"settings": {"index": {"uuid": ""}}}},
        {"articles": {"settings": {"index": {"uuid": None}}}},
    ],
)
def test_missing_index_identity_preserves_state_without_opening_a_pit(response):
    client = FakeElasticsearch([document()])
    state = {}
    sync(client, state)
    before = deepcopy(state)
    client.indices.get_settings = Mock(return_value=response)
    with pytest.raises((RuntimeError, ValueError)):
        sync(client, state)
    assert state == before and len(client.closed) == 1


def test_index_recreated_during_a_scan_aborts_before_publishing():
    client = FakeElasticsearch([document("1"), document("2")])
    state = {}
    sync(client, state)
    before = deepcopy(state)
    client.documents = [document("2", seq=2, body="Changed")]
    original = client.search

    def recreate(**kwargs):
        result = original(**kwargs)
        client.index_uuids["articles"] = "recreated-uuid"
        return result

    client.search = recreate
    with pytest.raises(RuntimeError, match="indices changed"):
        sync(client, state)
    assert state == before and len(client.closed) == 2


def test_unknown_inventory_index_aborts_the_entire_snapshot():
    client = FakeElasticsearch([document(index="unexpected")])
    client.indices.get_settings = Mock(
        return_value={"articles": {"settings": {"index": {"uuid": "uuid-articles"}}}}
    )
    state = {}
    with pytest.raises(RuntimeError, match="index selection changed"):
        sync(client, state)
    assert state == {} and client.closed


def test_duplicate_inventory_identity_with_different_sort_values_is_refused():
    client = FakeElasticsearch([document("1"), document("2")])
    original = client.search

    def duplicate(**kwargs):
        result = original(**kwargs)
        hits = result["hits"]["hits"]
        if kwargs["source"] is False and len(hits) == 2:
            hits[1]["_id"] = hits[0]["_id"]
        return result

    client.search = duplicate
    state = {}
    with pytest.raises(RuntimeError, match="Duplicate document identity"):
        sync(client, state)
    assert state == {} and client.closed


def test_out_of_order_hits_in_one_page_cannot_drive_deletions():
    client = FakeElasticsearch([document("1"), document("2")])
    original = client.search

    def out_of_order(**kwargs):
        result = original(**kwargs)
        result["hits"]["hits"].reverse()
        return result

    client.search = out_of_order
    state = {}
    with pytest.raises(RuntimeError, match="pagination did not advance"):
        sync(client, state)
    assert state == {} and client.closed


def test_index_recreation_fetches_unchanged_revisions_but_deduplicates_same_content():
    client = FakeElasticsearch([document()])
    state = {}
    sync(client, state)
    client.index_uuids["articles"] = "new-uuid"
    client.requests.clear()
    assert sync(client, state) == []
    assert any(request["source"] is not False for request in client.requests)
    client.requests.clear()
    assert sync(client, state) == []
    assert all(request["source"] is False for request in client.requests)


@pytest.mark.parametrize("sort_value", [True, 1.5, None])
def test_invalid_date_sort_type_cannot_authorize_deletions(sort_value):
    client = FakeElasticsearch([document()])
    original = client.search

    def invalid(**kwargs):
        result = original(**kwargs)
        for hit in result["hits"]["hits"]:
            hit["sort"][0] = sort_value
        return result

    client.search = invalid
    state = {}
    with pytest.raises(ValueError, match="integer epoch milliseconds"):
        sync(client, state)
    assert state == {} and client.closed


def test_remote_cluster_skip_is_rejected_even_with_successful_local_shards():
    client = FakeElasticsearch([document()])
    original = client.search

    def skipped(**kwargs):
        result = original(**kwargs)
        result["_clusters"] = {"skipped": 1}
        return result

    client.search = skipped
    state = {}
    with pytest.raises(RuntimeError, match="skipped a remote cluster"):
        sync(client, state)
    assert state == {} and client.closed
