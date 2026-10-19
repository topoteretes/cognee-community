"""Regressions for the October review: ownership, useful prose, and bounded extraction."""

import copy
import json

import httpx
import pytest
from test_postman import FakeClient, collection, snapshot, source

from cognee_community_connector_postman import (
    PostmanAuthError,
    PostmanDataError,
    PostmanRateLimitError,
    postman_source,
)
from cognee_community_connector_postman.postman import PostmanClient, flatten_collection


def test_benign_values_survive_but_auth_and_explicit_values_do_not():
    tree = collection()
    tree["variable"] = [{"key": "format", "value": "application/json"}]
    request = tree["item"][0]["request"]
    request.update(
        header=[
            {"key": "X-Client", "value": "service"},
            {"key": "Content-Type", "value": "application/json"},
            {"key": "X-Api-Key", "value": "actual-header-credential"},
        ],
        auth={"type": "bearer", "bearer": [{"key": "token", "value": "actual-auth-credential"}]},
        body={"raw": "service"},
        description=(
            "The service returns application/json. actual-header-credential "
            "actual-auth-credential explicit-private"
        ),
    )
    rows = flatten_collection(tree, "1-c", "ns", redact_values=["explicit-private"])
    rendered = json.dumps(rows)
    assert "The service returns application/json" in rendered
    for secret in ("actual-header-credential", "actual-auth-credential", "explicit-private"):
        assert secret not in rendered


def test_default_deletion_and_explicit_deselection_even_after_access_loss():
    client = FakeClient()
    _, state = snapshot(client)
    client.inventory = {}
    rows, state = snapshot(client, state)
    assert rows == [] and not state["collections"]
    client = FakeClient()
    _, state = snapshot(client, collection_ids=["1-c"])
    client.inventory = {}
    rows, state = snapshot(
        client, state, collection_ids=[], missing_collection_policy="require_confirmation"
    )
    assert rows == [] and not state["collections"]


def test_selection_edits_replace_same_table_keep_ids_and_sibling_table(tmp_path):
    import dlt
    from dlt.destinations import sqlalchemy

    client = FakeClient()
    client.inventory = {f"1-{key}": {"updatedAt": "now", "owner": "1"} for key in "abc"}
    pipeline = dlt.pipeline(
        pipeline_name="selection_edits",
        pipelines_dir=str(tmp_path / "pipelines"),
        destination=sqlalchemy(credentials=f"sqlite:///{tmp_path}/db.sqlite"),
        dataset_name="dataset",
    )

    def run(selection, resource="main"):
        src = source(client, collection_ids=selection, resource_name=resource)
        pipeline.run(src)
        with pipeline.sql_client() as sql:
            rows = sql.execute_sql(f'SELECT id, content FROM "{src.name}" ORDER BY id')
        return src.name, rows

    name, initial = run(["1-a", "1-b"])
    sibling, sibling_rows = run(["1-a"], "sibling")
    after_name, changed = run(["1-b", "1-c"])
    assert after_name == name and name != sibling
    assert len(changed) == 4
    assert {row[0] for row in changed if "1-b" in row[1]} == {
        row[0] for row in initial if "1-b" in row[1]
    }
    assert all("1-a" not in row[1] for row in changed)
    assert run([])[1] == []
    with pipeline.sql_client() as sql:
        assert sql.execute_sql(f'SELECT id, content FROM "{sibling}" ORDER BY id') == sibling_rows


def test_docs_only_collection_and_empty_folder_overviews_move_and_retire():
    tree = collection([])
    tree["item"] = [
        {"id": "folder-id", "name": "Guide", "description": "Folder guide", "item": []},
        {"name": "Empty", "item": []},
    ]
    rows = flatten_collection(tree, "1-c", "ns")
    assert len(rows) == 2
    folder = next(row for row in rows if "Guide" in row["title"])
    moved = copy.deepcopy(tree)
    moved["item"] = [{"name": "Parent", "item": [moved["item"][0]]}]
    moved_rows = flatten_collection(moved, "1-c", "ns")
    assert folder["id"] == next(row["id"] for row in moved_rows if "Guide" in row["title"])
    tree["info"]["description"] = ""
    tree["item"] = []
    assert flatten_collection(tree, "1-c", "ns") == []


def test_endpoint_metadata_default_and_provenance_exclude_values():
    tree = collection()
    req = tree["item"][0]["request"]
    req["url"] = {
        "raw": "https://user:private-pass@example.invalid/v1/orders/:id?token=query-private#fragment-private",
        "query": [{"key": "token", "value": "query-private"}],
    }
    req["header"] = [{"key": "Accept", "value": "private-header-value"}]
    default = flatten_collection(tree, "1-c", "ns", include_endpoint_metadata=False)
    assert "orders" not in json.dumps(default)
    rows = flatten_collection(tree, "1-c", "ns", include_endpoint_metadata=True)
    text = json.dumps(rows)
    assert "example.invalid/v1/orders/:id" in text and "Query names: token" in text
    assert "Header names: Accept" in text
    for secret in ("private-pass", "query-private", "fragment-private", "private-header-value"):
        assert secret not in text
    assert rows[0]["url"] == "https://go.postman.co/collection/1-c"
    req["url"] = "{{baseUrl}}/v1/orders/:id?expand=private-query"
    assert "{{baseUrl}}/v1/orders/:id" in json.dumps(
        flatten_collection(tree, "1-c", "ns", include_endpoint_metadata=True)
    )


@pytest.mark.parametrize("total", [0.5, -1, True, float("nan"), float("inf")])
def test_invalid_numeric_total_withholds_snapshot(total):
    client = FakeClient()
    get = client.get

    def altered(path, **params):
        result = get(path, **params)
        if path == "/collections":
            result["meta"]["total"] = total
        return result

    client.get = altered
    with pytest.raises(PostmanDataError):
        snapshot(client)


def test_integral_float_total_is_accepted_and_nochange_cost_is_three_calls():
    client = FakeClient()
    get = client.get

    def altered(path, **params):
        result = get(path, **params)
        if path == "/collections":
            result["meta"]["total"] = float(result["meta"]["total"])
        return result

    client.get = altered
    _, state = snapshot(client)
    client.calls.clear()
    snapshot(client, state)
    # build_snapshot excludes factory /me: one inventory and one workspace check.
    assert len(client.calls) == 2


def test_cancellation_before_requests_and_before_publish_does_not_commit(tmp_path):
    import dlt
    from dlt.destinations import sqlalchemy

    client = FakeClient()
    pipeline = dlt.pipeline(
        pipeline_name="cancel",
        pipelines_dir=str(tmp_path / "pipelines"),
        destination=sqlalchemy(credentials=f"sqlite:///{tmp_path}/db.sqlite"),
        dataset_name="dataset",
    )
    pipeline.run(source(client))
    saved = copy.deepcopy(pipeline.state["sources"])
    client.calls.clear()

    def cancelled():
        raise RuntimeError("Synthetic cancellation")

    with pytest.raises(Exception, match="Synthetic cancellation"):
        pipeline.run(source(client, check_active=cancelled))
    assert not client.calls and pipeline.state["sources"] == saved
    for cutoff in (4, 5):  # before yield, and after yield before cache mutation
        checkpoints = 0

        def before_publish(stop=cutoff):
            nonlocal checkpoints
            checkpoints += 1
            if checkpoints == stop:
                cancelled()

        with pytest.raises(Exception, match="Synthetic cancellation"):
            pipeline.run(source(client, check_active=before_publish))
        assert pipeline.state["sources"] == saved


def test_service_alias_and_typed_http_errors_and_global_wait_budget():
    src = postman_source(
        resource_name="postman",
        service=FakeClient(),
        workspace_id="w",
        account_id="1",
        corpus_owner="o",
        dataset_name="d",
    )
    list(src)
    assert src.cognee_sync_stats["scanned"] == 1
    waits = []
    count = 0

    def handler(_):
        nonlocal count
        count += 1
        return httpx.Response(429 if count % 2 else 200, headers={"Retry-After": "2"}, json={})

    client = PostmanClient("key", transport=httpx.MockTransport(handler), sleep=waits.append)
    try:
        client.get("/me")
        client.get("/me")
        with pytest.raises(PostmanRateLimitError):
            client.get("/me")
        assert sum(waits) == 4 and max(waits) <= 2
    finally:
        client.close()
    for status in (401, 403):
        client = PostmanClient(
            "key", transport=httpx.MockTransport(lambda _, code=status: httpx.Response(code))
        )
        try:
            with pytest.raises(PostmanAuthError):
                client.get("/me")
        finally:
            client.close()


@pytest.mark.parametrize(
    "url", ["//user:private-pass@example.com/v1/orders", "user:private-pass@example.com/v1/orders"]
)
def test_schemeless_endpoint_strips_userinfo(url):
    tree = collection()
    tree["item"][0]["request"]["url"] = url
    text = json.dumps(flatten_collection(tree, "c", "ns", include_endpoint_metadata=True))
    assert "private-pass" not in text and "user:" not in text
    assert "example.com/v1/orders" in text


def test_structured_endpoint_without_raw():
    tree = collection()
    tree["item"][0]["request"]["url"] = {
        "protocol": "https",
        "host": ["api", "example", "com"],
        "path": ["v1", "orders"],
    }
    assert "https://api.example.com/v1/orders" in json.dumps(
        flatten_collection(tree, "c", "ns", include_endpoint_metadata=True)
    )


@pytest.mark.parametrize("kind", ["transport", "server"])
def test_non_quota_retry_exhaustion_is_not_rate_limit(kind):
    def handler(request):
        if kind == "transport":
            raise httpx.ConnectError("private transport detail", request=request)
        return httpx.Response(503)

    client = PostmanClient("key", transport=httpx.MockTransport(handler), sleep=lambda _: None)
    try:
        with pytest.raises(Exception) as exc:
            client.get("/me")
        assert not isinstance(exc.value, PostmanRateLimitError)
        assert "private" not in str(exc.value)
    finally:
        client.close()


@pytest.mark.parametrize(
    "status,field", [(401, "failed_auth"), (429, "failed_rate_limit"), (200, "failed_data")]
)
def test_typed_failure_statistics(status, field):
    from dlt.extract.exceptions import ResourceExtractionError

    def handler(request):
        if request.url.path == "/me":
            return httpx.Response(200, json={"user": {"id": "1"}})
        return httpx.Response(status, headers={"Retry-After": "60"}, json=[])

    client = PostmanClient("key", transport=httpx.MockTransport(handler))
    src = source(client)
    try:
        with pytest.raises(ResourceExtractionError):
            list(src)
        assert src.cognee_sync_stats["failed"] == 1
        assert src.cognee_sync_stats[field] == 1
        assert src.cognee_sync_stats["documents"] == 0
        assert all(type(value) is int for value in src.cognee_sync_stats.values())
    finally:
        client.close()


def test_minimal_and_explicit_account_factories_share_verified_identity(tmp_path):
    import dlt
    from dlt.destinations import sqlalchemy

    client = FakeClient()
    pipeline = dlt.pipeline(
        pipeline_name="minimal_account",
        pipelines_dir=str(tmp_path / "pipelines"),
        destination=sqlalchemy(credentials=f"sqlite:///{tmp_path}/db.sqlite"),
        dataset_name="chosen_dataset",
    )

    def minimal(**kwargs):
        return postman_source(
            workspace_id="w", service=client, resource_name="connection", **kwargs
        )

    src = minimal()
    assert src.name == minimal(account_id="1").name
    pipeline.run(src)
    assert pipeline.state["sources"][src.name]["postman_snapshot"]["account_id"] == "1"
    with pytest.raises(PostmanAuthError):
        minimal(account_id="other")
    get = client.get
    client.get = lambda path, **params: (
        {"user": {"id": "other"}} if path == "/me" else get(path, **params)
    )
    assert minimal().name != src.name
