import copy
import json

import httpx
import pytest

from cognee_community_connector_postman.postman import (
    PostmanClient,
    PostmanSyncError,
    build_snapshot,
    flatten_collection,
    postman_source,
)


def collection(items=None):
    return {
        "info": {"name": "API", "description": "Collection docs"},
        "item": items
        if items is not None
        else [
            {
                "id": "r1",
                "name": "Read",
                "request": {"method": "GET", "description": "Read documentation"},
            },
            {
                "id": "r2",
                "name": "Write",
                "request": {"method": "POST", "description": "Write documentation"},
            },
        ],
    }


class FakeClient:
    def __init__(self):
        self.tree = collection()
        self.inventory = {"1-c": {"updatedAt": "2026-10-08T01:00:00Z", "owner": "1"}}
        self.calls = []
        self.fail_tree = False
        self.workspace_id = "w"

    def get(self, path, **params):
        self.calls.append((path, params))
        if path == "/me":
            return {"user": {"id": "1"}}
        if path == "/workspaces/w":
            return {"workspace": {"id": self.workspace_id}}
        if path == "/collections":
            rows = [{"uid": uid, **value} for uid, value in sorted(self.inventory.items())]
            offset, limit = params["offset"], params["limit"]
            return {
                "collections": rows[offset : offset + limit],
                "meta": {"offset": offset, "limit": limit, "total": len(rows)},
            }
        if path.startswith("/collections/"):
            if self.fail_tree:
                raise PostmanSyncError("HTTP 403")
            return {"collection": copy.deepcopy(self.tree)}
        raise AssertionError(path)


def snapshot(client, state=None, **kwargs):
    return build_snapshot(
        client, state or {}, workspace_id="w", account_id="1", namespace="namespace", **kwargs
    )


def test_cursor_move_and_inherited_documentation():
    client = FakeClient()
    rows, state = snapshot(client)
    assert len(rows) == 2
    client.calls.clear()
    again, state2 = snapshot(client, state)
    assert rows == again and state == state2
    assert not any(path.startswith("/collections/") for path, _ in client.calls)
    client.tree["info"]["description"] = "New collection documentation"
    original = client.tree["item"][0]
    client.tree["item"] = [{"name": "Folder", "description": "Folder docs", "item": [original]}]
    client.inventory["1-c"]["updatedAt"] = "later"
    changed, _ = snapshot(client, state)
    assert len(changed) == 1
    old = next(row for row in rows if "Read" in row["title"])
    assert changed[0]["id"] == old["id"]
    assert "Folder docs" in changed[0]["content"]
    assert "New collection documentation" in changed[0]["content"]
    assert "Folder" in changed[0]["title"]


def test_collection_disappearance_requires_confirmation_and_final_empty():
    client = FakeClient()
    _, state = snapshot(client)
    client.inventory = {}
    saved = copy.deepcopy(state)
    with pytest.raises(PostmanSyncError, match="disappeared"):
        snapshot(client, state, missing_collection_policy="require_confirmation")
    assert state == saved
    rows, next_state = snapshot(client, state, confirmed_deleted=["1-c"])
    assert rows == [] and next_state == {"collections": {}, "removed_collections": ["1-c"]}


def test_explicit_missing_selection_never_silently_erases():
    with pytest.raises(PostmanSyncError):
        snapshot(FakeClient(), collection_ids=["nonexistent"])


def test_live_confirmation_is_rejected():
    with pytest.raises(PostmanSyncError, match="still visible"):
        snapshot(FakeClient(), confirmed_deleted=["1-c"])


def test_invalid_workspace_and_failed_tree_preserve_state():
    client = FakeClient()
    _, state = snapshot(client)
    saved = copy.deepcopy(state)
    client.workspace_id = "wrong"
    with pytest.raises(PostmanSyncError, match="Workspace"):
        snapshot(client, state)
    client.workspace_id = "w"
    client.inventory["1-c"]["updatedAt"] = "changed"
    client.fail_tree = True
    with pytest.raises(PostmanSyncError):
        snapshot(client, state)
    assert state == saved


def test_multiple_pages():
    client = FakeClient()
    client.inventory["1-other"] = {"updatedAt": "now", "owner": "1"}
    rows, _ = snapshot(client, page_size=1)
    assert len(rows) == 4
    assert any(params.get("offset") == 1 for _, params in client.calls)


@pytest.mark.parametrize("fault", ["duplicate", "total", "missing_meta", "short", "offset"])
def test_pagination_fault_aborts(fault):
    client = FakeClient()
    get = client.get

    def bad(path, **params):
        result = get(path, **params)
        if path == "/collections":
            if fault == "duplicate":
                result["collections"] *= 2
            elif fault == "total":
                result["meta"]["total"] = -1
            elif fault == "missing_meta":
                del result["meta"]
            elif fault == "short":
                result["collections"] = []
            else:
                result["meta"]["offset"] = 123
        return result

    client.get = bad
    with pytest.raises(PostmanSyncError):
        snapshot(client)


def test_nonconverging_membership_aborts():
    client = FakeClient()
    get = client.get
    counter = 0

    def unstable(path, **params):
        nonlocal counter
        result = get(path, **params)
        if path == "/collections":
            counter += 1
            result["collections"][0]["updatedAt"] = str(counter)
        return result

    client.get = unstable
    with pytest.raises(PostmanSyncError, match="during tree"):
        snapshot(client)


def test_tree_fetch_race_aborts():
    client = FakeClient()
    get = client.get

    def raced(path, **params):
        result = get(path, **params)
        if path.startswith("/collections/"):
            client.inventory["1-c"]["updatedAt"] = "raced"
        return result

    client.get = raced
    with pytest.raises(PostmanSyncError, match="during tree"):
        snapshot(client)


def test_duplicate_missing_ids_and_deep_nesting():
    item = {"name": "same", "request": {"method": "GET"}}
    tree = collection([copy.deepcopy(item), copy.deepcopy(item)])
    rows = flatten_collection(tree, "c", "ns")
    assert len({row["id"] for row in rows}) == 2
    assert rows == flatten_collection(tree, "c", "ns")
    for entry in tree["item"]:
        entry["id"] = "duplicate"
    assert len({row["id"] for row in flatten_collection(tree, "c", "ns")}) == 2
    for _ in range(30):
        tree["item"] = [{"name": "Folder", "item": tree["item"]}]
    assert len(flatten_collection(tree, "c", "ns")) == 2


def test_redaction_in_rendered_rows_and_cached_state():
    client = FakeClient()
    request = client.tree["item"][0]["request"]
    request.update(
        {
            "auth": {"token": "seed-auth-secret"},
            "header": [{"key": "Authorization", "value": "seed-header-secret"}],
            "body": {"raw": "seed-body-secret"},
            "url": "https://user:seed-url-secret@example.com/?secret=seed-query-secret",
        }
    )
    request["description"] = (
        "seed-auth-secret seed-header-secret seed-body-secret PMAK-abc-123 "
        "token=seed-query-secret Bearer examplecredential seed-explicit-secret"
    )
    rows, state = snapshot(client, redact_values=["seed-explicit-secret", "seed-body-secret"])
    serialized = json.dumps([rows, state])
    for secret in [
        "seed-auth-secret",
        "seed-header-secret",
        "seed-body-secret",
        "seed-url-secret",
        "seed-query-secret",
        "PMAK-abc-123",
        "examplecredential",
        "seed-explicit-secret",
    ]:
        assert secret not in serialized
    assert "[REDACTED]" in serialized
    assert '"auth"' not in serialized and '"body"' not in serialized


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
def test_http_errors_have_bounded_retries_and_no_secret_output(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": "secret-response"})

    client = PostmanClient(
        "secret-key", transport=httpx.MockTransport(handler), sleep=lambda _: None
    )
    try:
        with pytest.raises(PostmanSyncError) as error:
            client.get("/me")
        assert "secret" not in str(error.value)
        assert len(calls) == (3 if status in (429, 500) else 1)
        assert calls[0].headers["X-Api-Key"] == "secret-key"
    finally:
        client.close()


def test_monthly_exhaustion_stops_next_call():
    client = PostmanClient(
        "key",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"user": {"id": "1"}}, headers={"RateLimit-Remaining-Month": "0"}
            )
        ),
    )
    try:
        client.get("/me")
        with pytest.raises(PostmanSyncError, match="monthly"):
            client.get("/me")
    finally:
        client.close()


def source(client, **kwargs):
    kwargs.setdefault("resource_name", "postman")
    return postman_source(
        client=client,
        workspace_id="w",
        account_id="1",
        corpus_owner="owner",
        dataset_name="dataset",
        **kwargs,
    )


def test_namespace_isolation_and_rotation():
    client = FakeClient()
    first = source(client, api_key="old")
    assert first.name == source(client, api_key="new").name
    other = postman_source(
        resource_name="postman",
        client=client,
        workspace_id="w",
        account_id="1",
        corpus_owner="other",
        dataset_name="dataset",
    )
    assert first.name != other.name
    assert first.name == source(client, collection_ids=["1-c"]).name
    assert first.name != source(client, resource_name="sibling").name
    assert first.cognee_document_source == "postman"


def test_real_dlt_sqlite_lifecycle_empty_replace_and_replay(tmp_path):
    import dlt
    from dlt.destinations import sqlalchemy

    client = FakeClient()
    pipeline = dlt.pipeline(
        pipeline_name="postman_test",
        pipelines_dir=str(tmp_path / "pipeline"),
        destination=sqlalchemy(credentials=f"sqlite:///{tmp_path}/data.sqlite"),
        dataset_name="postman_test",
    )

    def run(**kwargs):
        src = source(client, **kwargs)
        info = pipeline.run(src)
        assert not info.has_failed_jobs
        return src.name

    name = run()
    with pipeline.sql_client() as sql:
        rows = sql.execute_sql(f'SELECT id, content FROM "{name}" ORDER BY id')
    assert len(rows) == 2
    client.calls.clear()
    assert run() == name
    assert not any(path.startswith("/collections/") for path, _ in client.calls)
    with pipeline.sql_client() as sql:
        assert sql.execute_sql(f'SELECT id, content FROM "{name}" ORDER BY id') == rows
    client.tree["info"]["description"] = "Timestamp-independent refreshed docs"
    assert run(force_refresh=True) == name
    with pipeline.sql_client() as sql:
        refreshed = sql.execute_sql(f'SELECT id, content FROM "{name}" ORDER BY id')
    assert [row[0] for row in refreshed] == [row[0] for row in rows]
    assert all("Timestamp-independent refreshed docs" in row[1] for row in refreshed)
    client.tree["item"] = []
    client.tree["info"]["description"] = ""
    client.inventory["1-c"]["updatedAt"] = "empty"
    run()
    with pipeline.sql_client() as sql:
        assert sql.execute_sql(f'SELECT id FROM "{name}"') == []
    run()
    with pipeline.sql_client() as sql:
        assert sql.execute_sql(f'SELECT id FROM "{name}"') == []


def test_real_dlt_extraction_failure_keeps_cursor_and_destination(tmp_path):
    import dlt
    from dlt.destinations import sqlalchemy

    client = FakeClient()
    pipeline = dlt.pipeline(
        pipeline_name="postman_failure",
        pipelines_dir=str(tmp_path / "pipeline"),
        destination=sqlalchemy(credentials=f"sqlite:///{tmp_path}/data.sqlite"),
        dataset_name="postman_test",
    )
    src = source(client)
    pipeline.run(src)
    saved = copy.deepcopy(pipeline.state)
    client.inventory["1-c"]["updatedAt"] = "changed"
    client.fail_tree = True
    from dlt.pipeline.exceptions import PipelineStepFailed

    with pytest.raises(PipelineStepFailed):
        pipeline.run(source(client))
    assert pipeline.state["sources"] == saved["sources"]
    with pipeline.sql_client() as sql:
        assert len(sql.execute_sql(f'SELECT id FROM "{src.name}"')) == 2
    client.fail_tree = False
    pipeline.run(source(client))
    assert pipeline.state["sources"] != saved["sources"]


def test_dlt_load_failure_retains_pending_snapshot_then_recovers(tmp_path, monkeypatch):
    import dlt
    from dlt.destinations import sqlalchemy

    client = FakeClient()
    pipeline = dlt.pipeline(
        pipeline_name="postman_load_failure",
        pipelines_dir=str(tmp_path / "pipeline"),
        destination=sqlalchemy(credentials=f"sqlite:///{tmp_path}/data.sqlite"),
        dataset_name="postman_test",
    )
    src = source(client)
    pipeline.run(src)
    client.tree["item"] = client.tree["item"][:1]
    client.inventory["1-c"]["updatedAt"] = "request-deleted"
    pipeline.extract(source(client))
    pipeline.normalize()
    pending = pipeline.list_normalized_load_packages()
    assert pending
    load = pipeline.load

    def interrupted_load(*args, **kwargs):
        raise RuntimeError("Injected outage before destination load")

    monkeypatch.setattr(pipeline, "load", interrupted_load)
    with pytest.raises(RuntimeError, match="Injected outage"):
        pipeline.run(source(client))
    assert pipeline.list_normalized_load_packages() == pending
    with pipeline.sql_client() as sql:
        assert len(sql.execute_sql(f'SELECT id FROM "{src.name}"')) == 2
    monkeypatch.setattr(pipeline, "load", load)
    # dlt first loads the retained package; source extraction can be deferred.
    pipeline.run(source(client))
    assert not pipeline.list_normalized_load_packages()
    with pipeline.sql_client() as sql:
        assert len(sql.execute_sql(f'SELECT id FROM "{src.name}"')) == 1
    client.calls.clear()
    pipeline.run(source(client))
    assert not any(path.startswith("/collections/") for path, _ in client.calls)


def test_redaction_policy_change_refetches_without_changing_identity():
    client = FakeClient()
    client.tree["item"][0]["request"]["description"] = "custom-confidential-text"
    rows, state = snapshot(client)
    filtered, _ = snapshot(client, state, redact_values=["custom-confidential-text"])
    assert [row["id"] for row in rows] == [row["id"] for row in filtered]
    assert "custom-confidential-text" not in json.dumps(filtered)


@pytest.mark.parametrize(
    "field,value",
    [
        ("collection_ids", "1-c"),
        ("collection_ids", [""]),
        ("collection_ids", [None]),
        ("collection_ids", ["1-c/other"]),
        ("confirmed_deleted", "1-c"),
        ("page_size", True),
        ("page_size", 1.5),
        ("max_pages", 0),
        ("redact_values", "secret"),
        ("redact_values", [123]),
    ],
)
def test_invalid_configuration_rejected_before_provider_calls(field, value):
    client = FakeClient()
    with pytest.raises(ValueError):
        source(client, **{field: value})
    assert client.calls == []


def test_invalid_api_key_never_appears_in_errors():
    with pytest.raises(ValueError) as error:
        PostmanClient("sensitive-key\nmalformed")
    assert "sensitive-key" not in str(error.value)


def test_schema_request_shorthand_and_item_documentation():
    tree = collection(
        [
            {
                "name": "Shorthand",
                "description": {"content": "Item docs"},
                "request": "https://example.invalid/?secret=not-rendered",
            }
        ]
    )
    rows = flatten_collection(tree, "c", "ns")
    assert len(rows) == 1 and "Item docs" in rows[0]["content"]
    assert "Method: GET" in rows[0]["content"]
    assert "not-rendered" not in json.dumps(rows)


def test_rendering_revision_invalidates_cache(monkeypatch):
    import importlib

    module = importlib.import_module("cognee_community_connector_postman.postman")
    client = FakeClient()
    _, state = snapshot(client)
    client.calls.clear()
    monkeypatch.setattr(module, "_RENDER_VERSION", module._RENDER_VERSION + 1)
    snapshot(client, state)
    assert any(path.startswith("/collections/") for path, _ in client.calls)


def test_redaction_values_order_is_canonical():
    client = FakeClient()
    _, state = snapshot(client, redact_values=["secret-a", "secret-b"])
    client.calls.clear()
    snapshot(client, state, redact_values=["secret-b", "secret-a"])
    assert not any(path.startswith("/collections/") for path, _ in client.calls)


def test_raw_headers_scripts_and_cookies_are_not_copied():
    tree = collection()
    request = tree["item"][0]["request"]
    request["header"] = "X-Secret: raw-header-secret"
    request["description"] = "raw-header-secret script-secret cookie-secret"
    tree["event"] = [{"script": {"exec": ["script-secret"]}}]
    tree["cookie"] = [{"value": "cookie-secret"}]
    serialized = json.dumps(flatten_collection(tree, "c", "ns", redact_values=["script-secret"]))
    for secret in ["raw-header-secret", "script-secret", "cookie-secret"]:
        assert secret not in serialized


@pytest.mark.parametrize("payload", [{"error": {"name": "limit"}}, ["unexpected"], None])
def test_http_200_error_or_invalid_shape_is_not_inventory(payload):
    client = PostmanClient(
        "key", transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    )
    try:
        with pytest.raises(PostmanSyncError):
            client.get("/collections")
    finally:
        client.close()


def test_retry_after_is_honored_and_redirect_is_not_followed():
    delays, calls = [], []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0.25"})
        return httpx.Response(200, json={"user": {"id": "1"}})

    client = PostmanClient("key", transport=httpx.MockTransport(handler), sleep=delays.append)
    try:
        assert client.get("/me")["user"]["id"] == "1"
        assert delays == [0.25] and len(calls) == 2
    finally:
        client.close()
    calls.clear()

    def redirect(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://example.invalid/collect"})

    client = PostmanClient("key", transport=httpx.MockTransport(redirect))
    try:
        with pytest.raises(PostmanSyncError):
            client.get("/me")
        assert len(calls) == 1
    finally:
        client.close()


def test_malformed_item_id_aborts_without_publishing():
    tree = collection()
    tree["item"][0]["id"] = {"not": "an ID"}
    with pytest.raises(PostmanSyncError, match="item ID"):
        flatten_collection(tree, "c", "ns")


def test_selected_collection_scope_does_not_remove_unselected_cached_data():
    # Selections have distinct namespaces. Within a fixed selection a missing
    # selected collection must block the snapshot rather than erase its records.
    client = FakeClient()
    client.inventory["1-other"] = {"updatedAt": "now", "owner": "1"}
    rows, state = snapshot(client, collection_ids=["1-c"])
    assert len(rows) == 2 and set(state["collections"]) == {"1-c"}
    client.inventory.pop("1-c")
    with pytest.raises(PostmanSyncError, match="disappeared"):
        snapshot(
            client, state, collection_ids=["1-c"], missing_collection_policy="require_confirmation"
        )


def test_redaction_preserves_metadata_words_but_removes_values():
    tree = collection()
    tree["variable"] = [{"key": "customer_token", "type": "string", "value": "private-value"}]
    tree["response"] = [{"name": "customer", "status": "customer", "body": "private-body"}]
    tree["item"][0]["name"] = "customer"
    tree["item"][0]["request"]["description"] = "customer string private-value private-body"
    rows = flatten_collection(tree, "1-c", "scope", redact_values=["private-body"])
    row = next(row for row in rows if "customer" in row["title"])
    assert "customer string" in row["content"]
    assert "private-value" not in row["content"]
    assert "private-body" not in row["content"]


def test_shorthand_url_credentials_are_redacted_from_inherited_documentation():
    tree = collection(
        [
            {
                "id": "r1",
                "name": "Read",
                "request": "https://user:private-password@example.invalid/?token=private-query",
            }
        ]
    )
    tree["info"]["description"] = "Read docs private-password private-query"
    rows = flatten_collection(tree, "1-c", "scope")
    assert "Read docs" in rows[0]["content"]
    assert "private-password" not in json.dumps(rows)
    assert "private-query" not in json.dumps(rows)


def test_confirmed_explicit_collection_removal_replays_empty():
    client = FakeClient()
    _, state = snapshot(client, collection_ids=["1-c"])
    client.inventory = {}
    rows, state = snapshot(
        client,
        state,
        collection_ids=["1-c"],
        missing_collection_policy="mirror_visibility",
        confirmed_deleted=["1-c"],
    )
    assert rows == [] and state["removed_collections"] == ["1-c"]
    again, state = snapshot(
        client, state, collection_ids=["1-c"], missing_collection_policy="mirror_visibility"
    )
    assert again == []
    client.inventory = {"1-c": {"updatedAt": "returned", "owner": "1"}}
    restored, state = snapshot(
        client, state, collection_ids=["1-c"], missing_collection_policy="mirror_visibility"
    )
    assert len(restored) == 2 and "removed_collections" not in state


def test_visibility_mirror_never_observed_uid_still_fails():
    with pytest.raises(PostmanSyncError, match="disappeared"):
        snapshot(
            FakeClient(), collection_ids=["typo"], missing_collection_policy="mirror_visibility"
        )


def test_confirmed_removal_is_persisted_without_repeated_confirmation():
    client = FakeClient()
    _, state = snapshot(client, collection_ids=["1-c"])
    client.inventory = {}
    _, state = snapshot(client, state, collection_ids=["1-c"], confirmed_deleted=["1-c"])
    rows, _ = snapshot(client, state, collection_ids=["1-c"])
    assert rows == []


def test_visibility_mirror_retains_other_collection():
    client = FakeClient()
    client.inventory["1-other"] = {"updatedAt": "now", "owner": "1"}
    _, state = snapshot(client)
    del client.inventory["1-c"]
    rows, state = snapshot(client, state, missing_collection_policy="mirror_visibility")
    assert len(rows) == 2 and set(state["collections"]) == {"1-other"}


def test_visibility_mirror_failed_workspace_preserves_state():
    client = FakeClient()
    _, state = snapshot(client)
    before = copy.deepcopy(state)
    client.inventory = {}
    client.workspace_id = "wrong"
    with pytest.raises(PostmanSyncError):
        snapshot(client, state, missing_collection_policy="mirror_visibility")
    assert state == before


def test_real_dlt_reappearance_clears_removal_acknowledgement(tmp_path):
    import dlt
    from dlt.destinations import sqlalchemy
    from dlt.pipeline.exceptions import PipelineStepFailed

    client = FakeClient()
    pipeline = dlt.pipeline(
        pipeline_name="acknowledgements",
        pipelines_dir=str(tmp_path / "pipeline"),
        destination=sqlalchemy(credentials=f"sqlite:///{tmp_path}/data.sqlite"),
        dataset_name="postman_test",
    )
    pipeline.run(source(client, collection_ids=["1-c"]))
    client.inventory = {}
    pipeline.run(source(client, collection_ids=["1-c"], confirmed_deleted=["1-c"]))
    pipeline.run(source(client, collection_ids=["1-c"]))
    client.inventory = {"1-c": {"updatedAt": "returned", "owner": "1"}}
    pipeline.run(source(client, collection_ids=["1-c"]))
    saved = copy.deepcopy(pipeline.state)
    client.inventory = {}
    with pytest.raises(PipelineStepFailed):
        pipeline.run(
            source(client, collection_ids=["1-c"], missing_collection_policy="require_confirmation")
        )
    assert pipeline.state["sources"] == saved["sources"]


@pytest.mark.parametrize(
    "field",
    [
        "collection_name",
        "collection_description",
        "folder_name",
        "folder_description",
        "item_name",
        "item_description",
        "request_description",
        "method",
    ],
)
def test_rendered_dependencies_and_timestamp_refresh(field):
    client = FakeClient()
    item = client.tree["item"][0]
    folder = {"name": "Original folder", "description": "Original folder docs", "item": [item]}
    client.tree["item"] = [folder]
    rows, state = snapshot(client)
    target, key = {
        "collection_name": (client.tree["info"], "name"),
        "collection_description": (client.tree["info"], "description"),
        "folder_name": (folder, "name"),
        "folder_description": (folder, "description"),
        "item_name": (item, "name"),
        "item_description": (item, "description"),
        "request_description": (item["request"], "description"),
        "method": (item["request"], "method"),
    }[field]
    target[key] = "PATCH" if field == "method" else "Updated dependency"
    # No timestamp signal: default reuse is intentional, full refresh reconciles it.
    unchanged, _ = snapshot(client, state)
    assert unchanged == rows
    refreshed, _ = snapshot(client, state, force_refresh=True)
    assert refreshed != rows
    assert refreshed[0]["id"] == rows[0]["id"]
    client.inventory["1-c"]["updatedAt"] = "advanced"
    changed, _ = snapshot(client, state)
    assert changed == refreshed


def test_snapshot_statistics_are_counts_only_and_reset():
    client = FakeClient()
    src = source(client)
    list(src)
    assert src.cognee_sync_stats == dict(
        scanned=1,
        skipped=0,
        fetched=1,
        documents=2,
        failed=0,
        failed_auth=0,
        failed_rate_limit=0,
        failed_data=0,
    )
    assert all(type(value) is int for value in src.cognee_sync_stats.values())
    assert all(
        resource.cognee_sync_stats is src.cognee_sync_stats for resource in src.resources.values()
    )
