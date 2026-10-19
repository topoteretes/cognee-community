"""Second external review regressions: privacy, guarded deletion and source contracts."""

import copy
import json

import httpx
import pytest
from test_postman import FakeClient, collection, snapshot, source

from cognee_community_connector_postman import PostmanDataError, PostmanSyncError, postman_source
from cognee_community_connector_postman.postman import PostmanClient, flatten_collection


def test_secret_variable_declared_auth_header_and_query_values_are_redacted():
    tree = collection()
    tree["variable"] = [{"key": "myVar", "value": "VARSEC11", "type": "secret"}]
    tree["auth"] = {
        "type": "apikey",
        "apikey": [
            {"key": "in", "value": "header"},
            {"key": "key", "value": "X-Custom-Access"},
            {"key": "value", "value": "AUTHSECRET99"},
        ],
    }
    request = tree["item"][0]["request"]
    request.update(
        url="https://example.invalid/orders?key=KSECRET88&other=OTHERSECRET77",
        header=[
            {"key": "X-Custom-Access", "value": "AUTHSECRET99"},
            {"key": "X-Auth", "value": "zzSECRETzz"},
        ],
        description="tok VARSEC11 AUTHSECRET99 KSECRET88 OTHERSECRET77 zzSECRETzz",
    )
    rows = flatten_collection(tree, "1-c", "ns")
    text = json.dumps(rows)
    for secret in ("VARSEC11", "AUTHSECRET99", "KSECRET88", "OTHERSECRET77", "zzSECRETzz"):
        assert secret not in text
    assert "X-Custom-Access" in text and "X-Auth" in text


def test_short_secrets_do_not_corrupt_words_versions_or_counts():
    tree = collection()
    tree["variable"] = [{"key": "api_token", "value": "1"}]
    request = tree["item"][0]["request"]
    request["auth"] = {"type": "bearer", "bearer": [{"key": "token", "value": "a"}]}
    request["description"] = "Read Retry 10 times, version 1.2; standalone a and 1."
    text = json.dumps(flatten_collection(tree, "1-c", "ns"))
    assert "Read Retry 10 times, version 1.2" in text
    assert "standalone a" not in text


def test_benign_header_and_query_prose_survives():
    tree = collection()
    tree["item"][0]["request"].update(
        url="https://example.invalid/?format=service",
        header=[{"key": "X-Client", "value": "service"}],
        description="The service returns application/json.",
    )
    assert "The service returns application/json" in json.dumps(flatten_collection(tree, "c", "ns"))


def test_default_endpoint_scrubs_path_token_and_has_real_request_provenance():
    tree = collection()
    item = tree["item"][0]
    item["id"] = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    item["request"]["url"] = "{{baseUrl}}/v1/hook/sk_live_PATHSECRET?token=QUERYSECRET"
    rows = flatten_collection(tree, "12345678-c", "ns")
    row = next(row for row in rows if row["title"].endswith("Read"))
    assert "{{baseUrl}}/v1/hook/" in row["content"]
    assert "PATHSECRET" not in json.dumps(rows) and "QUERYSECRET" not in json.dumps(rows)
    assert (
        row["url"] == "https://go.postman.co/request/12345678-a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    )


def test_consistent_empty_bulk_inventory_requires_confirmation():
    client = FakeClient()
    client.inventory.update(
        {"1-b": {"updatedAt": "now", "owner": "1"}, "1-c2": {"updatedAt": "now", "owner": "1"}}
    )
    _, state = snapshot(client)
    saved = copy.deepcopy(state)
    client.inventory = {}
    with pytest.raises(PostmanSyncError, match="Bulk"):
        snapshot(client, state)
    assert state == saved
    rows, _ = snapshot(client, state, confirmed_deleted=list(state["collections"]))
    assert rows == []


def test_real_dlt_factory_default_deletes_single_collection_and_guards_bulk(tmp_path):
    import dlt
    from dlt.destinations import sqlalchemy
    from dlt.pipeline.exceptions import PipelineStepFailed

    client = FakeClient()
    pipeline = dlt.pipeline(
        pipeline_name="bulk_guard",
        pipelines_dir=str(tmp_path / "pipelines"),
        destination=sqlalchemy(credentials=f"sqlite:///{tmp_path}/db.sqlite"),
        dataset_name="dataset",
    )
    src = source(client)
    pipeline.run(src)
    client.inventory = {}
    pipeline.run(source(client))  # Public factory default, not builder default.
    with pipeline.sql_client() as sql:
        assert sql.execute_sql(f'SELECT id FROM "{src.name}"') == []
    client.inventory = {f"1-{key}": {"updatedAt": "now", "owner": "1"} for key in "abc"}
    pipeline.run(source(client))
    saved = copy.deepcopy(pipeline.state["sources"])
    client.inventory = {}
    with pytest.raises(PipelineStepFailed):
        pipeline.run(source(client))
    assert pipeline.state["sources"] == saved
    with pipeline.sql_client() as sql:
        assert len(sql.execute_sql(f'SELECT id FROM "{src.name}"')) == 6


def test_resource_name_is_required_and_three_call_nochange_contract(tmp_path):
    import dlt
    from dlt.destinations import sqlalchemy

    client = FakeClient()
    with pytest.raises(TypeError, match="resource_name"):
        postman_source(workspace_id="w", service=client)
    assert not client.calls
    pipeline = dlt.pipeline(
        pipeline_name="request_count",
        pipelines_dir=str(tmp_path / "pipelines"),
        destination=sqlalchemy(credentials=f"sqlite:///{tmp_path}/db.sqlite"),
        dataset_name="dataset",
    )
    pipeline.run(source(client))
    client.calls.clear()
    pipeline.run(source(client))
    assert len(client.calls) == 3
    assert [path for path, _ in client.calls] == ["/me", "/workspaces/w", "/collections"]


def test_malformed_query_has_typed_failure_and_unexpected_failures_count(monkeypatch, caplog):
    client = FakeClient()
    client.tree["item"][0]["request"]["url"] = {"raw": "x", "query": None}
    src = source(client, resource_name="malformed_query_case")
    with pytest.raises(Exception, match="Malformed endpoint query"):
        list(src)
    assert src.cognee_sync_stats["failed"] == src.cognee_sync_stats["failed_data"] == 1
    import cognee_community_connector_postman.postman as module

    def unexpected(*args, **kwargs):
        raise RuntimeError("synthetic-private-error-detail")

    monkeypatch.setattr(module, "flatten_collection", unexpected)
    src = source(FakeClient(), resource_name="unexpected_failure_case")
    with pytest.raises(Exception, match="synthetic-private-error-detail"):
        list(src)
    assert src.cognee_sync_stats["failed"] == 1
    assert "Postman extraction failed" in caplog.text
    assert "synthetic-private-error-detail" not in caplog.text


def test_client_and_host_checkpoints_are_not_duplicated():
    fake, checks, requests = FakeClient(), [], []

    def checkpoint():
        checks.append(1)

    def handler(request):
        requests.append(request)
        params = {
            k: int(v) if k in {"limit", "offset"} else v for k, v in request.url.params.items()
        }
        return httpx.Response(200, json=fake.get(request.url.path, **params))

    client = PostmanClient("key", transport=httpx.MockTransport(handler), check_active=checkpoint)
    try:
        list(source(client, check_active=checkpoint))
        assert len(checks) == len(requests) + 2  # publication checkpoints, no duplicate HTTP checks
    finally:
        client.close()


def test_name_only_item_is_malformed_but_empty_folder_is_valid():
    tree = collection([{"name": "not-a-request"}])
    with pytest.raises(PostmanDataError, match="neither request"):
        flatten_collection(tree, "c", "ns")
    tree["item"] = [{"name": "empty", "item": []}]
    flatten_collection(tree, "c", "ns")


@pytest.mark.parametrize("raw_header", [True, False])
def test_declared_custom_auth_header_value_differs_from_placeholder(raw_header):
    tree = collection()
    tree["auth"] = {
        "type": "apikey",
        "apikey": [
            {"key": "in", "value": "header"},
            {"key": "key", "value": "X-Custom-Access"},
            {"key": "value", "value": "{{apiKey}}"},
        ],
    }
    req = tree["item"][0]["request"]
    req["header"] = (
        "X-Custom-Access: distinct-actual-credential"
        if raw_header
        else [{"key": "X-Custom-Access", "value": "distinct-actual-credential"}]
    )
    req["description"] = "Copied distinct-actual-credential here"
    assert "distinct-actual-credential" not in json.dumps(flatten_collection(tree, "c", "ns"))
