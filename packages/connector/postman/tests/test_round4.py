"""External review counterexamples: useful prose, private fields and lazy guarded sync."""

import copy
import json

import pytest
from test_postman import FakeClient, collection, snapshot, source

from cognee_community_connector_postman import PostmanSyncError, postman_source
from cognee_community_connector_postman.postman import flatten_collection


def rendered(request):
    tree = collection()
    tree["item"][0]["request"].update(request)
    return json.dumps(flatten_collection(tree, "1-c", "ns"))


@pytest.mark.parametrize("structured", [False, True])
def test_query_sorts_dates_regions_and_enums_remain_useful(structured):
    values = {
        "sort": "created_at_desc",
        "date": "2024-01-01",
        "format": "JSON_API",
        "region": "EU-WEST-1",
    }
    url = "https://example.invalid/?" + "&".join(f"{k}={v}" for k, v in values.items())
    if structured:
        url = {"raw": url, "query": [{"key": k, "value": v} for k, v in values.items()]}
    text = rendered({"url": url, "description": "Results " + " ".join(values.values())})
    assert all(v in text for v in values.values())
    assert "?sort=" not in text


def test_short_secrets_do_not_replace_prose_articles_or_hosts():
    tree = collection()
    tree["variable"] = [{"key": "x", "value": "a", "type": "secret"}]
    req = tree["item"][0]["request"]
    req.update(
        url="https://a/b",
        description="Read the docs a lot; a banana and a bear.",
        auth={"type": "bearer", "bearer": [{"key": "token", "value": "a"}]},
    )
    text = json.dumps(flatten_collection(tree, "c", "ns"))
    assert "Read the docs a lot; a banana and a bear." in text
    assert "https://a/b" in text


@pytest.mark.parametrize("header", ["X-Api-Signature", "X-Session", "X-Client-Key", "X-Unusual"])
@pytest.mark.parametrize("raw", [False, True])
def test_custom_header_values_do_not_leak(header, raw):
    val = "PrivateHeader987AbCdEf123456789"
    headers = f"{header}: {val}" if raw else [{"key": header, "value": val}]
    assert val not in rendered({"header": headers, "description": f"Copied {val} here"})


@pytest.mark.parametrize(
    "body",
    [
        {"mode": "raw", "raw": '{"password":"BODYPW55"}'},
        {"mode": "urlencoded", "urlencoded": [{"key": "password", "value": "BODYPW55"}]},
        {"password": "BODYPW55"},
    ],
)
def test_sensitive_body_values_repeated_in_prose_do_not_leak(body):
    assert "BODYPW55" not in rendered({"body": body, "description": "Copied BODYPW55 here"})


@pytest.mark.parametrize("val", ["abcdefghij", "ABC12345"])
def test_sensitive_sig_query_is_not_subject_to_entropy_floor(val):
    assert val not in rendered(
        {"url": f"https://example.invalid/?sig={val}", "description": f"Copied {val} here"}
    )


@pytest.mark.parametrize(
    "url,secret",
    [
        ("https://example.invalid/hook/ghp_abc123XYZ", "ghp_abc123XYZ"),
        ("https://hooks.slack.com/services/T00000000/B00000000/WebhookSecret", "WebhookSecret"),
    ],
)
def test_known_endpoint_credentials_are_removed(url, secret):
    assert secret not in rendered({"url": url})


def test_absent_endpoint_omits_empty_metadata_labels_and_never_guesses_request_owner():
    tree = collection()
    tree["item"][0]["id"] = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    rows = flatten_collection(tree, "123-c", "ns")
    assert all(
        label not in rows[0]["content"]
        for label in ("Endpoint template:", "Query names:", "Header names:")
    )
    assert rows[0]["url"] == "https://go.postman.co/collection/123-c"


def test_single_cached_collection_empty_listing_refuses_and_lists_uid():
    client = FakeClient()
    _, state = snapshot(client)
    saved = copy.deepcopy(state)
    client.inventory = {}
    with pytest.raises(PostmanSyncError, match="1-c"):
        snapshot(client, state)
    assert state == saved
    assert snapshot(client, state, confirmed_deleted=["1-c"])[0] == []


def test_pending_removal_budget_does_not_expire(monkeypatch):
    import cognee_community_connector_postman.postman as module

    now = 1000
    monkeypatch.setattr(module.time, "time", lambda: now)
    client = FakeClient()
    client.inventory = {f"1-{k}": {"updatedAt": "now", "owner": "1"} for k in "abcd"}
    _, state = snapshot(client)
    del client.inventory["1-a"]
    _, state = snapshot(client, state)
    saved = copy.deepcopy(state)
    del client.inventory["1-b"]
    with pytest.raises(PostmanSyncError, match="1-b"):
        snapshot(client, state)
    assert state == saved
    now += 86400
    with pytest.raises(PostmanSyncError, match="1-b"):
        snapshot(client, state)
    _, state = snapshot(client, state, confirmed_deleted=["1-a", "1-b"])
    assert set(state["collections"]) == {"1-c", "1-d"}


def test_explicit_missing_selection_requires_confirmation_even_with_visible_sibling():
    client = FakeClient()
    client.inventory["1-b"] = {"updatedAt": "now", "owner": "1"}
    _, state = snapshot(client, collection_ids=["1-c"])
    del client.inventory["1-c"]
    with pytest.raises(PostmanSyncError, match="1-c"):
        snapshot(client, state, collection_ids=["1-c"])
    assert snapshot(client, state, collection_ids=["1-c"], confirmed_deleted=["1-c"])[0] == []


def test_source_construction_has_no_io_or_checkpoints_and_auth_failures_count(caplog):
    client, checks = FakeClient(), []
    src = postman_source(
        service=client,
        workspace_id="w",
        account_id="wrong",
        check_active=lambda: checks.append(1),
        resource_name="lazy_auth",
    )
    assert not client.calls and not checks
    with pytest.raises(Exception, match="account differs"):
        list(src)
    assert src.cognee_sync_stats["failed_auth"] == src.cognee_sync_stats["failed"] == 1
    assert "PostmanAuthError" in caplog.text


def test_unexpected_me_failure_is_counted_and_logged_without_private_message(caplog):
    client = FakeClient()

    def fail(*args, **kwargs):
        raise OSError("private-response-do-not-log")

    client.get = fail
    src = source(client, resource_name="lazy_transport")
    with pytest.raises(Exception, match="private-response-do-not-log"):
        list(src)
    assert src.cognee_sync_stats["failed"] == 1
    assert "OSError" in caplog.text and "private-response-do-not-log" not in caplog.text


def test_two_named_sources_keep_same_owner_different_selections_and_account_binding(tmp_path):
    import dlt
    from dlt.destinations import sqlalchemy
    from dlt.pipeline.exceptions import PipelineStepFailed

    client = FakeClient()
    client.inventory["1-b"] = {"updatedAt": "now", "owner": "1"}
    pipeline = dlt.pipeline(
        pipeline_name="round4_siblings",
        pipelines_dir=str(tmp_path / "pipelines"),
        destination=sqlalchemy(credentials=f"sqlite:///{tmp_path}/db.sqlite"),
        dataset_name="dataset",
    )
    left = source(client, resource_name="left", collection_ids=["1-c"])
    right = source(client, resource_name="right", collection_ids=["1-b"])
    pipeline.run(left)
    pipeline.run(right)
    pipeline.run(source(client, resource_name="left", collection_ids=[]))
    with pipeline.sql_client() as sql:
        assert sql.execute_sql(f'SELECT id FROM "{left.name}"') == []
        assert len(sql.execute_sql(f'SELECT id FROM "{right.name}"')) == 2
    saved = copy.deepcopy(pipeline.state["sources"])
    get = client.get
    client.get = lambda path, **params: (
        {"user": {"id": "other"}} if path == "/me" else get(path, **params)
    )
    with pytest.raises(PipelineStepFailed):
        pipeline.run(source(client, resource_name="right", collection_ids=["1-b"]))
    assert pipeline.state["sources"] == saved


@pytest.mark.parametrize(
    "prefix", ["https://HOOKS.SLACK.COM", "https://hooks.slack.com:443", "//hooks.slack.com"]
)
def test_slack_webhook_url_variants_do_not_leak(prefix):
    assert "WebhookSecret" not in rendered(
        {"url": prefix + "/services/T00000000/B00000000/WebhookSecret"}
    )
