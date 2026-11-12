"""Scheduled deletion safety and explicit credential/prose counterexamples."""

import copy
import json

import pytest
from test_postman import FakeClient, collection, snapshot, source

from cognee_community_connector_postman import PostmanDataError, PostmanSyncError
from cognee_community_connector_postman.postman import flatten_collection

HEX = "a1b2c3d4e5f67890a1b2c3d4e5f67890"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"


def text_for(req, **kwargs):
    tree = collection()
    tree["item"][0]["request"].update(req)
    return json.dumps(flatten_collection(tree, "1-c", "ns", **kwargs))


@pytest.mark.parametrize("raw", [False, True])
def test_benign_header_values_remain_in_prose(raw):
    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Content-Encoding": "gzip",
        "X-Api-Version": "2024-01-01",
        "X-Region": "EU-WEST-1",
    }
    value = (
        "\n".join(f"{k}: {v}" for k, v in headers.items())
        if raw
        else [{"key": k, "value": v} for k, v in headers.items()]
    )
    text = text_for({"header": value, "description": "Use " + " ".join(headers.values())})
    assert all(v in text for v in headers.values())


@pytest.mark.parametrize("token", [HEX, JWT])
@pytest.mark.parametrize(
    "location",
    ["query", "header", "path", "structured_path", "form", "raw_form", "raw_text", "json"],
)
def test_known_token_shapes_under_neutral_names_are_removed(token, location):
    req = {"description": f"Copied {token} here"}
    if location == "query":
        req["url"] = "https://example.invalid/?ref=" + token
    elif location == "header":
        req["header"] = [{"key": "X-Reference", "value": token}]
    elif location == "path":
        req["url"] = "https://example.invalid/hook/" + token
    elif location == "structured_path":
        req["url"] = {"protocol": "https", "host": ["example", "invalid"], "path": ["hook", token]}
    elif location == "form":
        req["body"] = {"mode": "urlencoded", "urlencoded": [{"key": "ref", "value": token}]}
    elif location == "json":
        req["body"] = {"mode": "raw", "raw": json.dumps({"ref": token})}
    elif location == "raw_form":
        req["body"] = {"mode": "raw", "raw": "code=" + token}
    else:
        req["body"] = {"mode": "raw", "raw": "opaque value " + token}
    assert token not in text_for(req)


@pytest.mark.parametrize(
    "raw", ["password=RAWFORM77", "password: RAWFORM77", "db_pwd=RAWFORM77", "db_pass=RAWFORM77"]
)
def test_non_json_sensitive_body_fields_are_collected(raw):
    assert "RAWFORM77" not in text_for(
        {"body": {"mode": "raw", "raw": raw}, "description": "Copied RAWFORM77 here"}
    )


def test_urlencoded_code_value_and_token_auth_scheme():
    assert "FORMCODE99" not in text_for(
        {
            "body": {"mode": "urlencoded", "urlencoded": [{"key": "code", "value": "FORMCODE99"}]},
            "description": "Copied FORMCODE99 here",
        }
    )
    text = text_for(
        {
            "header": [{"key": "Authorization", "value": "Token AuthSecret987"}],
            "description": "Copied AuthSecret987; Authorization: Token x",
        }
    )
    assert "AuthSecret987" not in text and "Token x" not in text


@pytest.mark.parametrize("value", ["a", "123", "true", "false", "short77"])
def test_short_values_do_not_globally_destroy_prose_even_if_explicit(value):
    prose = f"This is {value}; keep {value} unchanged."
    text = text_for(
        {"body": {"raw": json.dumps({"password": value})}, "description": prose},
        redact_values=[value],
    )
    assert prose in text


def test_pending_budget_survives_arbitrarily_spaced_syncs_and_reappearance(monkeypatch):
    import cognee_community_connector_postman.postman as module

    clock = 1000
    monkeypatch.setattr(module.time, "time", lambda: clock)
    client = FakeClient()
    client.inventory = {f"1-{k}": {"owner": "1", "updatedAt": "now"} for k in "abcdef"}
    _, state = snapshot(client)
    del client.inventory["1-a"]
    _, state = snapshot(client, state)
    assert state.get("pending_removals") == ["1-a"]
    del client.inventory["1-b"]
    saved = copy.deepcopy(state)
    for elapsed in [361, 3600, 86400, 864000]:
        clock += elapsed
        with pytest.raises(PostmanSyncError, match="1-a.*1-b"):
            snapshot(client, state)
        assert state == saved
    client.inventory["1-a"] = {"owner": "1", "updatedAt": "returned"}
    _, state = snapshot(client, state)
    assert state["pending_removals"] == ["1-b"] and "1-a" in state["collections"]
    _, state = snapshot(client, state, confirmed_deleted=["1-b"])
    assert state["pending_removals"] == []


def test_legacy_removal_state_without_at_migrates_instead_of_resetting_budget():
    client = FakeClient()
    _, state = snapshot(client)
    state["automatic_removals"] = [{"uid": "1-old"}]
    _, state = snapshot(client, state)
    assert state["pending_removals"] == ["1-old"]
    assert "automatic_removals" not in state


@pytest.mark.parametrize(
    "field,value",
    [("automatic_removals", [{}]), ("pending_removals", None), ("pending_removals", [1])],
)
def test_malformed_removal_state_is_typed_and_never_mutated(field, value):
    client = FakeClient()
    _, state = snapshot(client)
    state[field] = value
    saved = copy.deepcopy(state)
    with pytest.raises(PostmanDataError, match="removal state"):
        snapshot(client, state)
    assert state == saved


def test_stale_confirmation_cannot_block_returning_collection_and_acks_are_pruned():
    client = FakeClient()
    _, state = snapshot(client, collection_ids=["1-c"])
    original = copy.deepcopy(client.inventory)
    client.inventory = {}
    _, state = snapshot(client, state, collection_ids=["1-c"], confirmed_deleted=["1-c"])
    client.inventory = original
    rows, state = snapshot(client, state, collection_ids=["1-c"], confirmed_deleted=["1-c"])
    assert len(rows) == 2 and "removed_collections" not in state
    client.inventory = {}
    _, state = snapshot(client, state, collection_ids=["1-c"], confirmed_deleted=["1-c"])
    _, state = snapshot(client, state, collection_ids=[])
    assert state == {"collections": {}, "pending_removals": []}


def test_real_sql_pending_budget_survives_delayed_loads_and_confirmed_recovery(
    tmp_path, monkeypatch
):
    import dlt
    from dlt.destinations import sqlalchemy
    from dlt.pipeline.exceptions import PipelineStepFailed

    import cognee_community_connector_postman.postman as module

    client = FakeClient()
    client.inventory = {f"1-{k}": {"owner": "1", "updatedAt": "now"} for k in "abcdef"}
    pipeline = dlt.pipeline(
        pipeline_name="permanent_budget",
        pipelines_dir=str(tmp_path / "pipelines"),
        destination=sqlalchemy(credentials=f"sqlite:///{tmp_path}/db.sqlite"),
        dataset_name="dataset",
    )
    src = source(client)
    pipeline.run(src)
    del client.inventory["1-a"]
    pipeline.run(source(client))
    saved = copy.deepcopy(pipeline.state["sources"])
    del client.inventory["1-b"]
    future = module.time.time() + 864000
    monkeypatch.setattr(module.time, "time", lambda: future)
    with pytest.raises(PipelineStepFailed, match="1-a.*1-b"):
        pipeline.run(source(client))
    assert pipeline.state["sources"] == saved
    with pipeline.sql_client() as sql:
        assert len(sql.execute_sql(f'SELECT id FROM "{src.name}"')) == 10
    pipeline.run(source(client, confirmed_deleted=["1-a", "1-b"]))
    with pipeline.sql_client() as sql:
        assert len(sql.execute_sql(f'SELECT id FROM "{src.name}"')) == 8


def test_noncanonical_jwt_header_and_plain_domain_distinguished():
    import base64

    header = base64.urlsafe_b64encode(b'{ "typ": "JWT", "alg": "HS256" }').decode().rstrip("=")
    token = header + ".eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature123456"
    assert token not in text_for(
        {"body": {"raw": json.dumps({"ref": token})}, "description": "Copied " + token}
    )
    assert "www.example.com" in text_for(
        {
            "url": "https://example.invalid/?ref=www.example.com",
            "description": "See www.example.com",
        }
    )


@pytest.mark.parametrize("history", [None, [], [{"uid": "1-a", "at": 1}]])
def test_expired_legacy_acknowledgements_keep_budget_consumed(history):
    client = FakeClient()
    client.inventory = {f"1-{key}": {"owner": "1", "updatedAt": "now"} for key in "abc"}
    _, state = snapshot(client)
    state.pop("pending_removals", None)  # Legacy releases did not persist this marker.
    state["collections"].pop("1-a")
    state["removed_collections"] = ["1-a"]
    if history is not None:
        state["automatic_removals"] = history
    del client.inventory["1-a"]
    del client.inventory["1-b"]
    saved = copy.deepcopy(state)
    with pytest.raises(PostmanSyncError, match="1-a.*1-b"):
        snapshot(client, state)
    assert state == saved
    rows, state = snapshot(client, state, confirmed_deleted=["1-a", "1-b"])
    assert len(rows) == 2 and state["pending_removals"] == []
    assert "removed_collections" not in state


@pytest.mark.parametrize("token", [HEX, JWT])
@pytest.mark.parametrize(
    "payload",
    [
        lambda token: {"header": token},
        lambda token: {"query": [token]},
        lambda token: {"auth": {"type": token}},
        lambda token: {"path": [{"header": token}]},
        lambda token: {"url": "https://example.invalid/hook/" + token},
        lambda token: {"url": "example.invalid/hook/" + token},
        lambda token: {"nested": {"query": {"header": [token]}}},
    ],
)
def test_json_payload_keys_never_dispatch_as_postman_schema(token, payload):
    assert token not in text_for(
        {
            "body": {"mode": "raw", "raw": json.dumps(payload(token))},
            "description": "Copied " + token,
        }
    )


def test_body_payload_authorization_text_and_benign_collision_fields():
    assert "BodySecret987" not in text_for(
        {
            "body": {"raw": json.dumps({"header": "Authorization: Token BodySecret987"})},
            "description": "Copied BodySecret987",
        }
    )
    prose = "Use service, EU-WEST-1, no-cache and 2024-01-01."
    assert prose in text_for(
        {
            "body": {
                "raw": json.dumps(
                    {
                        "header": "service",
                        "query": ["EU-WEST-1"],
                        "auth": {"type": "no-cache"},
                        "path": ["2024-01-01"],
                    }
                )
            },
            "description": prose,
        }
    )
