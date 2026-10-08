from copy import deepcopy

import dlt
import pytest
from conftest import FakeElasticsearch, document

from cognee_community_connector_elasticsearch import elasticsearch_source
from cognee_community_connector_elasticsearch.elasticsearch import _complete, _sync


def sync(client, state, **overrides):
    options = {
        "index": "articles",
        "query": {"match_all": {}},
        "updated_field": "updated_at",
        "fields": ["title", "body"],
        "title_field": "title",
        "page_size": 2,
        "keep_alive": "2m",
        "scope": "test-scope",
    }
    options.update(overrides)
    return _sync(client, state, **options)


def test_more_than_10000_documents_with_tied_dates():
    client = FakeElasticsearch(document(str(i)) for i in range(10017))
    state = {}
    rows = sync(client, state, page_size=500)
    assert len(rows) == 10017
    assert len({row["id"] for row in rows}) == 10017
    assert state["watermark"] == 1000
    after = [request["search_after"] for request in client.requests if "search_after" in request]
    assert after and all(len(cursor) == 2 for cursor in after)
    assert len(client.closed) == 1
    assert all(not row["deleted"] for row in rows)


def test_updates_ties_backdated_arrivals_and_unchanged_runs():
    client = FakeElasticsearch([document("1", date=2000), document("2", date=1000)])
    state = {}
    sync(client, state)
    client.requests.clear()
    assert sync(client, state) == []
    assert all(request["source"] is False for request in client.requests)
    client.documents = [
        document("1", date=2000, seq=2, body="Edit at tied cursor"),
        document("2", date=500, seq=2, body="Backdated edit"),
        document("3", date=300, body="New old document"),
    ]
    rows = sync(client, state)
    assert len(rows) == 3
    assert state["watermark"] == 2000
    assert any("range" in str(request["query"]) for request in client.requests)
    assert sync(client, state) == []


def test_selected_content_unchanged_does_not_emit():
    client = FakeElasticsearch([document()])
    state = {}
    sync(client, state)
    client.documents[0]["seq"] += 1
    client.documents[0]["source"]["secret"] = "new private value"
    assert sync(client, state) == []
    assert next(iter(state["revisions"].values()))[1] == 2


def test_query_departure_and_final_document_delete():
    client = FakeElasticsearch([document("1"), document("2")])
    state = {}
    query = {"term": {"published": True}}
    sync(client, state, query=query)
    client.documents[0]["source"]["published"] = False
    rows = sync(client, state, query=query)
    assert rows == [{"id": 'test-scope:["articles","1"]', "deleted": True}]
    client.documents = []
    assert sync(client, state, query=query) == [
        {"id": 'test-scope:["articles","2"]', "deleted": True}
    ]
    assert state["revisions"] == state["fingerprints"] == {}
    assert sync(client, state, query=query) == []


def test_concrete_index_identity_and_selected_fields():
    rows = sync(FakeElasticsearch([document(index="a"), document(index="b")]), {})
    assert len({row["id"] for row in rows}) == 2
    assert all("secret" not in row["content"] for row in rows)
    assert all(row["title"] == "Guide" for row in rows)


@pytest.mark.parametrize("failure", ["transport", "partial", "delta"])
def test_failed_scan_preserves_state_and_closes_pit(failure):
    client = FakeElasticsearch([document("1"), document("2")])
    state = {}
    sync(client, state)
    before = deepcopy(state)
    client.documents = [document("2", seq=2, body="Edited")]
    client.requests.clear()
    if failure == "transport":
        client.fail_at = 2
    elif failure == "partial":
        client.partial_at = 1
    else:
        client.omit_delta = True
    with pytest.raises((ConnectionError, RuntimeError)):
        sync(client, state)
    assert state == before
    assert len(client.closed) == 2


@pytest.mark.parametrize(
    "response",
    [
        {"timed_out": True, "_shards": {"failed": 0}},
        {"terminated_early": True, "_shards": {"failed": 0}},
        {"_shards": {"failed": 1}},
        {},
        {"_shards": {"failed": 0}, "_clusters": {"skipped": 1}},
    ],
)
def test_incomplete_responses_refused(response):
    with pytest.raises(RuntimeError):
        _complete(response)


def test_missing_date_refused_without_advancing_state():
    client = FakeElasticsearch([document(date=None)])
    state = {}
    with pytest.raises(ValueError, match="date"):
        sync(client, state)
    assert state == {}
    assert client.closed


@pytest.mark.parametrize(
    "overrides",
    [
        {"source_id": ""},
        {"index": ""},
        {"index": "remote:index"},
        {"page_size": 0},
        {"page_size": 10001},
        {"page_size": True},
        {"fields": []},
        {"fields": "body"},
        {"query": {}},
    ],
)
def test_invalid_configuration(overrides):
    args = {"source_id": "test", "index": "articles", "client": FakeElasticsearch()}
    args.update(overrides)
    with pytest.raises(ValueError):
        elasticsearch_source(**args)


def test_missing_auth(monkeypatch):
    monkeypatch.delenv("ELASTICSEARCH_URL", raising=False)
    monkeypatch.delenv("ELASTICSEARCH_API_KEY", raising=False)
    with pytest.raises(ValueError, match="api_key"):
        elasticsearch_source(source_id="test", index="articles")


def test_scopes_and_markers():
    def source(**kwargs):
        return elasticsearch_source(
            source_id="test", index="articles", client=FakeElasticsearch(), **kwargs
        )

    original = source()
    assert original.cognee_document_source == "elasticsearch"
    assert original.cognee_pipeline_scope
    assert source(query={"term": {"published": True}}).name != original.name
    assert source(fields=["body"]).name != original.name
    assert source(api_key="rotated-key").name == original.name


def test_real_dlt_persisted_incremental_merge_and_final_delete(tmp_path):
    client = FakeElasticsearch([document("1"), document("2")])
    credentials = f"sqlite:///{tmp_path / 'staging.sqlite'}"

    def pipeline():
        return dlt.pipeline(
            pipeline_name="es_sync_test",
            pipelines_dir=str(tmp_path / "dlt"),
            destination=dlt.destinations.sqlalchemy(credentials=credentials),
            dataset_name="es_test",
        )

    def source():
        return elasticsearch_source(
            source_id="test", index="articles", client=client, fields=["title", "body"], page_size=1
        )

    table = next(iter(source().resources))

    def run():
        active = pipeline()
        active.run(source(), write_disposition="merge")
        with active.sql_client() as sql:
            return sql.execute_sql(f'SELECT id, content FROM "{table}" ORDER BY id')

    assert len(run()) == 2
    assert len(run()) == 2
    client.documents = [document("2", seq=2, date=2000, body="Updated")]
    rows = run()
    assert len(rows) == 1 and "Updated" in rows[0][1]
    client.documents = []
    assert run() == []
    assert run() == []


def test_failed_dlt_extraction_keeps_destination_and_cursor(tmp_path):
    client = FakeElasticsearch([document()])
    pipeline = dlt.pipeline(
        pipeline_name="failure_test",
        pipelines_dir=str(tmp_path / "dlt"),
        destination=dlt.destinations.sqlalchemy(
            credentials=f"sqlite:///{tmp_path / 'staging.sqlite'}"
        ),
        dataset_name="es_test",
    )

    def source():
        return elasticsearch_source(source_id="test", index="articles", client=client)

    table = next(iter(source().resources))
    pipeline.run(source())
    before = deepcopy(pipeline.state)
    client.documents = [document(seq=2, body="Recovery")]
    client.requests.clear()
    client.fail_at = 2
    with pytest.raises(Exception, match="simulated connection failure"):
        pipeline.run(source())
    with pipeline.sql_client() as sql:
        assert "Recovery" not in sql.execute_sql(f'SELECT content FROM "{table}"')[0][0]
    assert pipeline.state["sources"] == before["sources"]
    client.fail_at = None
    pipeline.run(source())
    with pipeline.sql_client() as sql:
        assert "Recovery" in sql.execute_sql(f'SELECT content FROM "{table}"')[0][0]


def test_epoch_millis_strings_advance_numerically():
    client = FakeElasticsearch([document("1", date="900"), document("2", date="1000")])
    assert len(sync(client, {}, page_size=1)) == 2


def test_replace_override_refused_before_any_api_read(tmp_path):
    client = FakeElasticsearch([document()])
    source = elasticsearch_source(source_id="test", index="articles", client=client)
    pipeline = dlt.pipeline(
        pipeline_name="unsafe_disposition",
        pipelines_dir=str(tmp_path / "dlt"),
        destination="duckdb",
    )
    with pytest.raises(Exception, match="requires write_disposition"):
        pipeline.extract(source, write_disposition="replace")
    assert not client.requests
