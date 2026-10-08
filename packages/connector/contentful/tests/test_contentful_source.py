"""Exercise the public Contentful source against real dlt SQLite loads."""

import logging
import traceback
from copy import deepcopy

import dlt
import httpx
import pytest
from dlt.pipeline.exceptions import PipelineStepFailed
from fakes import FakeContentful

from cognee_community_connector_contentful import contentful_source


@pytest.fixture(autouse=True)
def no_credentials_or_waits(monkeypatch):
    for name in ("CONTENTFUL_SPACE_ID", "CONTENTFUL_DELIVERY_TOKEN", "CONTENTFUL_ENVIRONMENT_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DLT_TELEMETRY", "false")
    # Fault tests still exercise the complete retry loop without sleeping.
    import cognee_community_connector_contentful._http as contentful_http

    monkeypatch.setattr(contentful_http.time, "sleep", lambda seconds: None)


@pytest.fixture
def fake():
    provider = FakeContentful()
    yield provider
    provider.client.close()


@pytest.fixture
def pipeline(tmp_path):
    return dlt.pipeline(
        pipeline_name="contentful_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'staging.db'}"),
        dataset_name="contentful_test",
        pipelines_dir=str(tmp_path / "pipelines"),
    )


def _source(fake, **kwargs):
    return contentful_source(
        fake.space,
        token="SECRET_DELIVERY_TOKEN",
        environment=fake.environment,
        host=fake.host,
        client=fake.client,
        **kwargs,
    )


def _run(pipeline, fake, **kwargs):
    source = _source(fake, **kwargs)
    resource = next(iter(source.selected_resources.values()))
    table = resource.name
    result = pipeline.run(source)
    result.raise_on_failed_jobs()
    return table


def _rows(pipeline, table):
    with (
        pipeline.sql_client() as client,
        client.execute_query(f'SELECT id, title, content, url FROM "{table}"') as cursor,
    ):
        rows = cursor.fetchall()
        identifiers = [row[0] for row in rows]
        assert len(identifiers) == len(set(identifiers)), "Staging contains duplicate document IDs"
        return {
            row[0]: {"id": row[0], "title": row[1], "content": row[2], "url": row[3]}
            for row in rows
        }


def _text(rows):
    return "\n".join(row["content"] for row in rows.values())


def _sync_requests(fake):
    return [request for request in fake.requests if request.url.path.endswith("/sync")]


def _lookup_requests(fake, endpoint):
    return [request for request in fake.requests if f"/{endpoint}/" in request.url.path]


@pytest.mark.parametrize("parameter", ["token", "space_id"])
def test_missing_configuration_fails_before_any_request(fake, parameter):
    arguments = {"space_id": fake.space, "token": "test", "client": fake.client}
    arguments[parameter] = None
    with pytest.raises(ValueError):
        contentful_source(**arguments)
    assert fake.requests == []


@pytest.mark.parametrize("parameter", ["space_id", "environment"])
def test_explicit_empty_configuration_never_falls_back_to_environment(fake, monkeypatch, parameter):
    monkeypatch.setenv("CONTENTFUL_SPACE_ID", fake.space)
    monkeypatch.setenv("CONTENTFUL_ENVIRONMENT_ID", fake.environment)
    arguments = {
        "space_id": fake.space,
        "environment": fake.environment,
        "token": "test",
        "client": fake.client,
    }
    arguments[parameter] = ""
    with pytest.raises(ValueError):
        contentful_source(**arguments)
    assert fake.requests == []


@pytest.mark.parametrize("token", ["test\x00value", "test\x7fvalue"])
def test_token_control_characters_are_rejected_before_request(fake, token):
    with pytest.raises(ValueError):
        contentful_source(fake.space, token=token, client=fake.client)
    assert fake.requests == []


def test_environment_configuration_defaults_and_token_rotation(pipeline, fake, monkeypatch):
    monkeypatch.setenv("CONTENTFUL_SPACE_ID", fake.space)
    monkeypatch.setenv("CONTENTFUL_DELIVERY_TOKEN", "FIRST_TOKEN")
    monkeypatch.setenv("CONTENTFUL_ENVIRONMENT_ID", "master")
    source = contentful_source(client=fake.client)
    table = next(iter(source.selected_resources.values())).name
    pipeline.run(source)
    monkeypatch.setenv("CONTENTFUL_DELIVERY_TOKEN", "SECOND_TOKEN")
    second = contentful_source(client=fake.client)
    assert next(iter(second.selected_resources.values())).name == table
    pipeline.run(second)
    assert _sync_requests(fake)[-1].url.params["sync_token"] == "t1"
    assert fake.requests[0].headers["authorization"] == "Bearer FIRST_TOKEN"
    assert fake.requests[-1].headers["authorization"] == "Bearer SECOND_TOKEN"


@pytest.mark.parametrize("parameter", ["content_type_ids", "locales"])
def test_empty_selection_is_rejected(fake, parameter):
    with pytest.raises(ValueError):
        _source(fake, **{parameter: []})
    assert fake.requests == []


@pytest.mark.parametrize("parameter", ["space_id", "environment"])
@pytest.mark.parametrize("value", [".", ".."])
def test_dot_segments_are_rejected_before_http_path_normalization(fake, parameter, value):
    arguments = {"space_id": fake.space, "token": "test", "client": fake.client}
    arguments[parameter] = value
    with pytest.raises(ValueError):
        contentful_source(**arguments)
    assert fake.requests == []


@pytest.mark.parametrize(
    "host",
    [
        "example.com",
        "http://cdn.contentful.com",
        "https://cdn.contentful.com",
        "cdn.contentful.com:8443",
        "cdn.contentful.com.evil.test",
        "cdn.contentful.com@evil.test",
        "cdn.contentful.com/path",
    ],
)
def test_unsafe_hosts_are_rejected_before_request(fake, host):
    with pytest.raises(ValueError):
        contentful_source(fake.space, token="secret", host=host, client=fake.client)
    assert fake.requests == []


@pytest.mark.parametrize("host", ["cdn.contentful.com", "cdn.eu.contentful.com"])
def test_non_master_routing_and_legacy_sync_continuations(pipeline, host):
    fake = FakeContentful(environment="staging", host=host)
    fake.sync_pages["initial"] = {
        "items": [fake.entry("first", "FIRST_PAYLOAD")],
        "nextPageUrl": fake.sync_url("page-2", legacy=True),
    }
    fake.sync_pages["page-2"] = {
        "items": [fake.entry("second", "SECOND_PAYLOAD")],
        "nextSyncUrl": fake.sync_url("terminal", legacy=True),
    }
    try:
        table = _run(pipeline, fake)
        _run(pipeline, fake)
        assert len(_rows(pipeline, table)) == 2
        assert [request.url.params.get("sync_token") for request in _sync_requests(fake)] == [
            None,
            "page-2",
            "terminal",
        ]
        for request in fake.requests:
            assert request.url.scheme == "https"
            assert request.url.host == host
            assert request.url.path.startswith("/spaces/space/environments/staging/")
            assert request.headers["authorization"] == "Bearer SECRET_DELIVERY_TOKEN"
            assert "access_token" not in request.url.params
    finally:
        fake.client.close()


def test_explicit_standard_port_continuation_is_safe(pipeline, fake):
    fake.sync_pages["initial"] = {
        "items": [],
        "nextPageUrl": "https://cdn.contentful.com:443/spaces/space/sync?sync_token=page",
    }
    fake.delta("page", [], "terminal")
    _run(pipeline, fake)
    assert _sync_requests(fake)[-1].url.path == "/spaces/space/environments/master/sync"


@pytest.mark.parametrize(
    "continuation",
    [
        "http://cdn.contentful.com/spaces/space/sync?sync_token=stolen",
        "https://evil.test/spaces/space/sync?sync_token=stolen",
        "https://cdn.eu.contentful.com/spaces/space/sync?sync_token=stolen",
        "https://cdn.contentful.com:8443/spaces/space/sync?sync_token=stolen",
        "https://user:password@cdn.contentful.com/spaces/space/sync?sync_token=stolen",
        "https://cdn.contentful.com/spaces/other/sync?sync_token=stolen",
        "https://cdn.contentful.com/spaces/space/environments/other/sync?sync_token=stolen",
        "https://cdn.contentful.com/spaces/space/content_types?sync_token=stolen",
        "https://cdn.contentful.com/spaces/space/sync",
        "https://cdn.contentful.com/spaces/space/sync?sync_token=a&sync_token=b",
        "https://cdn.contentful.com/spaces/space/sync?sync_token=a#fragment",
    ],
)
def test_unsafe_continuations_abort_without_forwarding_credentials(pipeline, fake, continuation):
    fake.sync_pages["initial"] = {"items": [], "nextPageUrl": continuation}
    with pytest.raises(PipelineStepFailed):
        _run(pipeline, fake)
    assert len(_sync_requests(fake)) == 1
    assert all(request.url.host == fake.host for request in fake.requests)


def test_redirects_are_disabled_even_for_caller_client_with_follow_redirects(pipeline, fake):
    fake.client.close()
    fake.client = httpx.Client(transport=httpx.MockTransport(fake.handle), follow_redirects=True)
    fake.override = lambda request: httpx.Response(
        302, headers={"Location": "https://evil.test/steal"}
    )
    with pytest.raises(PipelineStepFailed):
        _run(pipeline, fake)
    assert len(fake.requests) == 1
    assert fake.requests[0].url.host == fake.host


def test_client_remains_caller_owned_after_success_and_failure(pipeline, fake):
    _run(pipeline, fake)
    assert not fake.client.is_closed
    fake.override = lambda request: httpx.Response(401, json={"error": "invalid"})
    with pytest.raises(PipelineStepFailed):
        _run(pipeline, fake)
    assert not fake.client.is_closed


def test_tokens_never_enter_logs_or_surfaced_exception(pipeline, fake, caplog):
    secret = "SECRET_DELIVERY_TOKEN"
    fake.override = lambda request: httpx.Response(
        401, json={"message": f"invalid access_token={secret}"}
    )
    with caplog.at_level(logging.DEBUG), pytest.raises(PipelineStepFailed) as captured:
        _run(pipeline, fake)
    assert secret not in caplog.text
    assert secret not in str(captured.value)
    assert secret not in "".join(traceback.format_exception(captured.value))


def test_initial_delta_empty_delta_and_final_document_deletion(pipeline, fake):
    fake.initial_items = [fake.entry("one", "ORIGINAL_PAYLOAD")]
    table = _run(pipeline, fake)
    original = _rows(pipeline, table)
    assert len(original) == 1
    fake.delta("t1", [fake.entry("one", "UPDATED_PAYLOAD", revision=2)], "t2")
    _run(pipeline, fake)
    changed = _rows(pipeline, table)
    assert changed.keys() == original.keys()
    assert "UPDATED_PAYLOAD" in _text(changed)
    assert "ORIGINAL_PAYLOAD" not in _text(changed)
    fake.delta("t2", [], "t3")
    _run(pipeline, fake)
    assert _rows(pipeline, table) == changed
    fake.delta("t3", [fake.deleted("one")], "t4")
    _run(pipeline, fake)
    assert _rows(pipeline, table) == {}
    _run(pipeline, fake)
    assert _rows(pipeline, table) == {}


def test_pagination_drains_all_pages_and_retains_terminal_token(pipeline, fake):
    fake.sync_pages["initial"] = {
        "items": [fake.entry("one", "FIRST_PAYLOAD")],
        "nextPageUrl": fake.sync_url("middle"),
    }
    fake.delta("middle", [fake.asset("two", "SECOND_PAYLOAD")], "terminal")
    table = _run(pipeline, fake)
    assert len(_rows(pipeline, table)) == 2
    _run(pipeline, fake)
    assert _sync_requests(fake)[-1].url.params["sync_token"] == "terminal"


def test_failed_later_sync_page_preserves_previous_rows_and_checkpoint(pipeline, fake):
    fake.initial_items = [fake.entry("one", "ORIGINAL_PAYLOAD")]
    table = _run(pipeline, fake)
    before = _rows(pipeline, table)
    fake.page("t1", [fake.entry("one", "UNCOMMITTED_PAYLOAD")], "failed-page")
    fake.override = lambda request: (
        httpx.Response(401, json={"sys": {"id": "AccessTokenInvalid"}})
        if request.url.params.get("sync_token") == "failed-page"
        else None
    )
    with pytest.raises(PipelineStepFailed):
        _run(pipeline, fake)
    assert _rows(pipeline, table) == before
    fake.override = None
    fake.delta("failed-page", [], "t2")
    requests_before = len(_sync_requests(fake))
    _run(pipeline, fake)
    assert _sync_requests(fake)[requests_before].url.params["sync_token"] == "t1"
    assert "UNCOMMITTED_PAYLOAD" in _text(_rows(pipeline, table))


def test_invalid_sync_token_keeps_memory_and_reuses_old_checkpoint(pipeline, fake):
    fake.initial_items = [fake.entry("one", "RETAINED_PAYLOAD")]
    table = _run(pipeline, fake)
    fake.override = lambda request: (
        httpx.Response(400, json={"sys": {"id": "InvalidQuery"}, "message": "bad token"})
        if request.url.params.get("sync_token") == "t1"
        else None
    )
    with pytest.raises(PipelineStepFailed):
        _run(pipeline, fake)
    assert "RETAINED_PAYLOAD" in _text(_rows(pipeline, table))
    fake.override = None
    _run(pipeline, fake)
    assert _sync_requests(fake)[-1].url.params["sync_token"] == "t1"


def test_filter_changes_refresh_and_remove_only_own_source_membership(pipeline, fake):
    fake.initial_items = [
        fake.entry("one", "ARTICLE_PAYLOAD"),
        fake.entry("two", "PRODUCT_PAYLOAD", content_type="product"),
        fake.asset("image", "ASSET_PAYLOAD"),
    ]
    fake.models = [fake.model(), fake.model("product", name="Product")]
    table = _run(pipeline, fake, source_id="catalog")
    independent = _run(pipeline, fake, source_id="archive")
    assert table != independent
    archive_before = _rows(pipeline, independent)
    assert len(_rows(pipeline, table)) == 5
    assert (
        _run(
            pipeline, fake, source_id="catalog", content_type_ids=["article"], include_assets=False
        )
        == table
    )
    narrowed = _text(_rows(pipeline, table))
    assert "ARTICLE_PAYLOAD" in narrowed
    assert "PRODUCT_PAYLOAD" not in narrowed
    assert "ASSET_PAYLOAD" not in narrowed
    assert len(_rows(pipeline, table)) == 2
    assert _rows(pipeline, independent) == archive_before
    _run(pipeline, fake, source_id="catalog")
    assert len(_rows(pipeline, table)) == 5
    assert _rows(pipeline, independent) == archive_before
    assert all("content_type" not in request.url.params for request in _sync_requests(fake))


def test_failed_selection_refresh_preserves_previous_selection(pipeline, fake):
    fake.initial_items = [fake.entry("one", "ARTICLE_PAYLOAD"), fake.asset("image", "IMAGE")]
    table = _run(pipeline, fake)
    before = _rows(pipeline, table)
    fake.override = lambda request: (
        httpx.Response(401, json={"error": "invalid"})
        if request.url.params.get("initial")
        else None
    )
    with pytest.raises(PipelineStepFailed):
        _run(pipeline, fake, include_assets=False)
    assert _rows(pipeline, table) == before
    fake.override = None
    _run(pipeline, fake)
    assert _sync_requests(fake)[-1].url.params.get("sync_token") == "t1"
    assert _rows(pipeline, table) == before


def test_locale_selection_retains_structure_links_and_localized_metadata(pipeline, fake):
    rich_text = {
        "nodeType": "document",
        "data": {},
        "content": [
            {
                "nodeType": "paragraph",
                "data": {},
                "content": [
                    {
                        "nodeType": "text",
                        "value": "RICH_TEXT_PAYLOAD",
                        "marks": [{"type": "bold"}],
                        "data": {},
                    }
                ],
            }
        ],
    }
    fake.initial_items = [
        fake.entry(
            "localized",
            "",
            fields={
                "title": {"en-US": "ENGLISH_PAYLOAD", "fr-FR": "FRENCH_PAYLOAD"},
                "body": {"fr-FR": rich_text},
                "reference": {
                    "fr-FR": {"sys": {"type": "Link", "linkType": "Entry", "id": "LINKED_ENTRY"}}
                },
            },
        )
    ]
    table = _run(pipeline, fake, locales=["fr-FR"])
    content = _text(_rows(pipeline, table))
    assert "fr-FR" in content and "FRENCH_PAYLOAD" in content
    assert "ENGLISH_PAYLOAD" not in content
    assert "RICH_TEXT_PAYLOAD" in content and "LINKED_ENTRY" in content
    assert "body" in content and "reference" in content
    assert "paragraph" in content and "bold" in content


def test_asset_metadata_normalizes_urls_without_fetching_binary(pipeline, fake):
    fake.initial_items = [fake.asset("picture", "ASSET_DESCRIPTION")]
    table = _run(pipeline, fake)
    content = _text(_rows(pipeline, table))
    for expected in ("ASSET_DESCRIPTION", "picture.png", "image/png", "640", "480", "1234"):
        assert expected in content
    assert "https://images.ctfassets.net/space/picture/image.png" in content
    assert all(request.url.host == fake.host for request in fake.requests)


def test_metadata_only_entry_change_does_not_churn_document_text(pipeline, fake):
    fake.initial_items = [fake.entry("one", "UNCHANGED_PAYLOAD")]
    table = _run(pipeline, fake)
    before = _rows(pipeline, table)
    fake.delta(
        "t1",
        [fake.entry("one", "UNCHANGED_PAYLOAD", revision=2, updated_at="2026-03-01T00:00:00Z")],
        "t2",
    )
    _run(pipeline, fake)
    assert _rows(pipeline, table) == before


def test_models_paginate_independently_and_edit_without_reimporting_entries(pipeline, fake):
    fake.initial_items = [fake.entry("one", "ENTRY_PAYLOAD")]
    fake.models = [fake.model(), fake.model("product", name="Product")]
    fake.model_page_size = 1
    table = _run(pipeline, fake)
    before = _rows(pipeline, table)
    assert fake.model_pass_count == 2
    assert len(before) == 3
    fake.models[0] = fake.model(name="EDITED_MODEL", revision=2)
    _run(pipeline, fake)
    after = _rows(pipeline, table)
    assert "EDITED_MODEL" in _text(after)
    entry_before = next(row for row in before.values() if "ENTRY_PAYLOAD" in row["content"])
    assert after[entry_before["id"]] == entry_before
    assert fake.model_pass_count == 4
    assert not _lookup_requests(fake, "entries")


def test_inventory_accepts_only_two_consecutive_matching_complete_passes(pipeline, fake):
    first = fake.model(name="FIRST_MODEL")
    current = fake.model(name="CURRENT_MODEL", revision=2)
    fake.model_passes = [[first], [current], [current]]
    table = _run(pipeline, fake)
    assert fake.model_pass_count == 3
    assert "CURRENT_MODEL" in _text(_rows(pipeline, table))
    assert "FIRST_MODEL" not in _text(_rows(pipeline, table))


def test_changing_model_inventory_aborts_without_loading_or_checkpointing(pipeline, fake):
    fake.initial_items = [fake.entry("one", "ORIGINAL_PAYLOAD")]
    fake.models = [fake.model()]
    table = _run(pipeline, fake)
    before = _rows(pipeline, table)
    fake.model_pass_count = 0
    fake.model_passes = [[fake.model(name=name)] for name in ("A", "B", "C")]
    fake.delta("t1", [fake.entry("one", "UNCOMMITTED_PAYLOAD")], "t2")
    with pytest.raises(PipelineStepFailed):
        _run(pipeline, fake)
    assert fake.model_pass_count == 3
    assert _rows(pipeline, table) == before
    fake.model_passes = []
    requests_before = len(_sync_requests(fake))
    _run(pipeline, fake)
    assert _sync_requests(fake)[requests_before].url.params["sync_token"] == "t1"


@pytest.mark.parametrize("fault", ["duplicate", "repeated_cursor", "missing_items", "bad_pages"])
def test_unreliable_model_inventory_never_deletes_previous_models(pipeline, fake, fault):
    fake.models = [fake.model()]
    table = _run(pipeline, fake)
    before = _rows(pipeline, table)

    def override(request):
        if not request.url.path.endswith("/content_types"):
            return None
        payloads = {
            "duplicate": {"items": [fake.model(), fake.model()], "pages": {}},
            "repeated_cursor": {
                "items": [],
                "pages": {"next": (f"{fake.url('content_types')}?pageNext=loop")},
            },
            "missing_items": {"pages": {}},
            "bad_pages": {"items": [], "pages": []},
        }
        return httpx.Response(200, json=payloads[fault])

    fake.override = override
    with pytest.raises(PipelineStepFailed):
        _run(pipeline, fake)
    assert _rows(pipeline, table) == before
    assert not _lookup_requests(fake, "content_types")


def test_model_disappearance_requires_confirmed_not_found(pipeline, fake):
    fake.models = [fake.model()]
    table = _run(pipeline, fake)
    fake.models = []
    _run(pipeline, fake)
    assert _rows(pipeline, table) == {}
    assert len(_lookup_requests(fake, "content_types")) == 1


@pytest.mark.parametrize("status", [200, 401, 503])
def test_incomplete_inventory_probe_preserves_rows_and_checkpoint(pipeline, fake, status):
    model = fake.model()
    fake.models = [model]
    table = _run(pipeline, fake)
    before = _rows(pipeline, table)
    fake.models = []
    fake.override = lambda request: (
        httpx.Response(status, json=model if status == 200 else {"error": "lookup failed"})
        if "/content_types/" in request.url.path
        else None
    )
    with pytest.raises(PipelineStepFailed):
        _run(pipeline, fake)
    assert _rows(pipeline, table) == before
    fake.override = None
    fake.models = [model]
    _run(pipeline, fake)
    assert _sync_requests(fake)[-1].url.params["sync_token"] == "t1"


@pytest.mark.parametrize("reverse", [False, True])
def test_cross_page_conflicts_resolve_authoritatively_with_all_locales(pipeline, fake, reverse):
    first = fake.entry("same", "OLD_PAYLOAD", updated_at="2026-01-01T00:00:00Z")
    deletion = fake.deleted("same", deleted_at="2026-02-01T00:00:00Z")
    current = fake.entry(
        "same", "REPUBLISHED_PAYLOAD", revision=1, updated_at="2026-03-01T00:00:00Z"
    )
    events = [first, deletion, current]
    if reverse:
        events.reverse()
    fake.sync_pages["initial"] = {"items": events[:1], "nextPageUrl": fake.sync_url("middle")}
    fake.delta("middle", events[1:], "terminal")
    fake.live[("Entry", "same")] = current
    table = _run(pipeline, fake)
    assert "REPUBLISHED_PAYLOAD" in _text(_rows(pipeline, table))
    assert "OLD_PAYLOAD" not in _text(_rows(pipeline, table))
    requests = _lookup_requests(fake, "entries")
    assert len(requests) == 1
    assert requests[0].url.params["locale"] == "*"
    assert requests[0].url.path == "/spaces/space/environments/master/entries/same"


def test_duplicate_events_and_cross_kind_ids_are_independent(pipeline, fake):
    entry = fake.entry("shared", "ENTRY_PAYLOAD")
    asset = fake.asset("shared", "ASSET_PAYLOAD")
    fake.initial_items = [entry, deepcopy(entry), asset, deepcopy(asset)]
    fake.models = [fake.model("shared", name="MODEL_PAYLOAD")]
    table = _run(pipeline, fake)
    assert len(_rows(pipeline, table)) == 3
    assert not _lookup_requests(fake, "entries")
    assert not _lookup_requests(fake, "assets")
    fake.delta("t1", [fake.deleted("shared", kind="Entry")], "t2")
    _run(pipeline, fake)
    text = _text(_rows(pipeline, table))
    assert "ENTRY_PAYLOAD" not in text
    assert "ASSET_PAYLOAD" in text and "MODEL_PAYLOAD" in text


def test_update_delete_republish_repeated_across_runs(pipeline, fake):
    fake.initial_items = [fake.entry("one", "FIRST_PAYLOAD")]
    table = _run(pipeline, fake)
    fake.delta("t1", [fake.entry("one", "EDITED_PAYLOAD", revision=2)], "t2")
    _run(pipeline, fake)
    assert "EDITED_PAYLOAD" in _text(_rows(pipeline, table))
    deleted = fake.deleted("one")
    fake.delta("t2", [deleted, deepcopy(deleted)], "t3")
    _run(pipeline, fake)
    assert _rows(pipeline, table) == {}
    republished = fake.entry(
        "one", "REPUBLISHED_PAYLOAD", revision=1, updated_at="2026-03-01T00:00:00Z"
    )
    fake.delta("t3", [republished, deepcopy(republished)], "t4")
    _run(pipeline, fake)
    assert "REPUBLISHED_PAYLOAD" in _text(_rows(pipeline, table))
    assert not _lookup_requests(fake, "entries")


@pytest.mark.parametrize("resolution", ["stale", "wrong_id", "wrong_environment", "failure"])
def test_contradictory_conflict_resolution_aborts_before_checkpoint(pipeline, fake, resolution):
    fake.initial_items = [fake.entry("one", "ORIGINAL_PAYLOAD")]
    table = _run(pipeline, fake)
    before = _rows(pipeline, table)
    fake.delta(
        "t1",
        [
            fake.entry("one", "EDITED_PAYLOAD", updated_at="2026-02-01T00:00:00Z"),
            fake.deleted("one", deleted_at="2026-03-01T00:00:00Z"),
        ],
        "t2",
    )
    resolved = fake.entry("wrong" if resolution == "wrong_id" else "one", "LOOKUP_PAYLOAD")
    if resolution == "wrong_environment":
        resolved["sys"]["environment"]["sys"]["id"] = "other"
    fake.live[("Entry", "one")] = resolved
    if resolution == "failure":
        fake.override = lambda request: (
            httpx.Response(503, json={"error": "unavailable"})
            if "/entries/" in request.url.path
            else None
        )
    with pytest.raises(PipelineStepFailed):
        _run(pipeline, fake)
    assert _rows(pipeline, table) == before
    fake.override = None
    fake.delta("t1", [], "recovered")
    requests_before = len(_sync_requests(fake))
    _run(pipeline, fake)
    assert _sync_requests(fake)[requests_before].url.params["sync_token"] == "t1"


def test_conflict_resolved_not_found_removes_document(pipeline, fake):
    fake.initial_items = [fake.entry("one", "ORIGINAL_PAYLOAD")]
    table = _run(pipeline, fake)
    fake.delta("t1", [fake.entry("one", "EDITED_PAYLOAD"), fake.deleted("one")], "t2")
    fake.live[("Entry", "one")] = None
    _run(pipeline, fake)
    assert _rows(pipeline, table) == {}


def test_conflict_not_found_cannot_erase_newer_republish_evidence(pipeline, fake):
    fake.initial_items = [fake.entry("one", "RETAINED_PAYLOAD")]
    table = _run(pipeline, fake)
    before = _rows(pipeline, table)
    fake.delta(
        "t1",
        [
            fake.deleted("one", deleted_at="2026-02-01T00:00:00Z"),
            fake.entry("one", "REPUBLISHED_PAYLOAD", updated_at="2026-03-01T00:00:00Z"),
        ],
        "t2",
    )
    fake.live[("Entry", "one")] = None
    with pytest.raises(PipelineStepFailed):
        _run(pipeline, fake)
    assert _rows(pipeline, table) == before
    fake.delta("t1", [], "recovered")
    requests_before = len(_sync_requests(fake))
    _run(pipeline, fake)
    assert _sync_requests(fake)[requests_before].url.params["sync_token"] == "t1"


def test_interrupted_load_keeps_package_and_recovers_before_fresh_extraction(
    pipeline, fake, monkeypatch
):
    fake.initial_items = [fake.entry("one", "ORIGINAL_PAYLOAD")]
    table = _run(pipeline, fake)
    fake.delta("t1", [fake.entry("one", "RECOVERED_PAYLOAD")], "t2")
    original_load = pipeline.load

    def interrupt_load(*args, **kwargs):
        raise RuntimeError("Injected interruption at destination load")

    monkeypatch.setattr(pipeline, "load", interrupt_load)
    with pytest.raises(RuntimeError, match="Injected interruption"):
        _run(pipeline, fake)
    assert pipeline.list_normalized_load_packages()
    assert "ORIGINAL_PAYLOAD" in _text(_rows(pipeline, table))
    requests_before = len(_sync_requests(fake))
    monkeypatch.setattr(pipeline, "load", original_load)
    _run(pipeline, fake)
    assert len(_sync_requests(fake)) == requests_before
    assert "RECOVERED_PAYLOAD" in _text(_rows(pipeline, table))
    assert not pipeline.list_normalized_load_packages()
    _run(pipeline, fake)
    assert _sync_requests(fake)[-1].url.params["sync_token"] == "t2"


@pytest.mark.parametrize(
    "fault",
    [
        "missing_items",
        "bad_items",
        "both_cursors",
        "no_cursor",
        "loop",
        "wrong_environment",
        "wrong_space",
        "unknown_kind",
        "bad_fields",
    ],
)
def test_malformed_sync_cycle_never_replaces_retained_staging(pipeline, fake, fault):
    fake.initial_items = [fake.entry("one", "RETAINED_PAYLOAD")]
    table = _run(pipeline, fake)
    before = _rows(pipeline, table)
    payload = {"items": [], "nextSyncUrl": fake.sync_url("t2")}
    if fault == "missing_items":
        del payload["items"]
    elif fault == "bad_items":
        payload["items"] = {"one": fake.entry("one", "BAD_PAYLOAD")}
    elif fault == "both_cursors":
        payload["nextPageUrl"] = fake.sync_url("page")
    elif fault == "no_cursor":
        del payload["nextSyncUrl"]
    elif fault == "loop":
        payload = {"items": [], "nextPageUrl": fake.sync_url("t1")}
    else:
        entity = fake.entry("one", "BAD_PAYLOAD")
        if fault in {"wrong_environment", "wrong_space"}:
            key = "environment" if fault == "wrong_environment" else "space"
            entity["sys"][key]["sys"]["id"] = "different"
        elif fault == "unknown_kind":
            entity["sys"]["type"] = "Unknown"
        else:
            entity["fields"] = []
        payload["items"] = [entity]
    fake.sync_pages["t1"] = payload
    with pytest.raises(PipelineStepFailed):
        _run(pipeline, fake)
    assert _rows(pipeline, table) == before
    fake.delta("t1", [], "recovered")
    requests_before = len(_sync_requests(fake))
    _run(pipeline, fake)
    assert _sync_requests(fake)[requests_before].url.params["sync_token"] == "t1"


@pytest.mark.parametrize("status", [429, 503])
def test_transient_errors_retry_and_rate_limit_reset_is_honored(
    pipeline, fake, monkeypatch, status
):
    import cognee_community_connector_contentful._http as contentful_http

    waits = []
    monkeypatch.setattr(contentful_http.time, "sleep", waits.append)
    calls = 0

    def override(request):
        nonlocal calls
        if not request.url.path.endswith("/sync"):
            return None
        calls += 1
        if calls == 1:
            return httpx.Response(status, headers={"X-Contentful-RateLimit-Reset": "2"})
        return None

    fake.override = override
    _run(pipeline, fake)
    assert calls == 2
    assert waits and all(0 <= delay <= 60 for delay in waits)
    if status == 429:
        assert waits[0] == 2


def test_retries_are_bounded_and_exception_contains_no_response_body(pipeline, fake):
    fake.override = lambda request: httpx.Response(503, text="SECRET_DELIVERY_TOKEN")
    with pytest.raises(PipelineStepFailed) as captured:
        _run(pipeline, fake)
    assert 1 < len(fake.requests) <= 6
    assert "SECRET_DELIVERY_TOKEN" not in str(captured.value)


@pytest.mark.parametrize("disposition", ["append", "replace"])
def test_effective_write_disposition_rejected_before_extraction(pipeline, fake, disposition):
    with pytest.raises(PipelineStepFailed):
        pipeline.run(_source(fake), write_disposition=disposition)
    assert fake.requests == []


@pytest.mark.parametrize("primary_key", [["id", "title"], ["title"], "title"])
def test_composite_or_alternate_primary_key_rejected_without_touching_memory(
    pipeline, fake, primary_key
):
    fake.initial_items = [fake.entry("one", "RETAINED_PAYLOAD")]
    table = _run(pipeline, fake)
    before = _rows(pipeline, table)
    requests_before = len(fake.requests)
    fake.delta("t1", [fake.entry("one", "WRONG_KEY_PAYLOAD")], "t2")
    with pytest.raises(PipelineStepFailed):
        pipeline.run(_source(fake), primary_key=primary_key)
    assert len(fake.requests) == requests_before
    assert _rows(pipeline, table) == before
    _run(pipeline, fake)
    assert _sync_requests(fake)[-1].url.params["sync_token"] == "t1"
    assert "WRONG_KEY_PAYLOAD" in _text(_rows(pipeline, table))


def test_scd2_merge_strategy_rejected_before_extraction(pipeline, fake):
    fake.initial_items = [fake.entry("one", "RETAINED_PAYLOAD")]
    table = _run(pipeline, fake)
    before = _rows(pipeline, table)
    requests_before = len(fake.requests)
    with pytest.raises(PipelineStepFailed):
        pipeline.run(_source(fake), write_disposition={"disposition": "merge", "strategy": "scd2"})
    assert len(fake.requests) == requests_before
    assert _rows(pipeline, table) == before


def test_normalized_selection_order_does_not_refresh_or_change_source_identity(pipeline, fake):
    first = _source(fake, content_type_ids=["product", "article"], locales=["fr-FR", "en-US"])
    first_table = next(iter(first.selected_resources.values())).name
    pipeline.run(first)
    second = _source(
        fake, content_type_ids=["article", "product", "article"], locales=["en-US", "fr-FR"]
    )
    assert next(iter(second.selected_resources.values())).name == first_table
    pipeline.run(second)
    assert _sync_requests(fake)[-1].url.params.get("sync_token") == "t1"
