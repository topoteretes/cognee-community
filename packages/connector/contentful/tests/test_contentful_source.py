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
        space_id=fake.space,
        token="SECRET_DELIVERY_TOKEN",
        resource_name=kwargs.pop(
            "resource_name", "contentful_" + kwargs.get("source_id", "default")
        ),
        environment=fake.environment,
        host=fake.host,
        service=fake.client,
        **kwargs,
    )


def _run(pipeline, fake, **kwargs):
    source = _source(fake, **kwargs)
    table = source.name
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
    arguments = {
        "space_id": fake.space,
        "token": "test",
        "resource_name": "contentful_default",
        "service": fake.client,
    }
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
        "resource_name": "contentful_default",
        "service": fake.client,
    }
    arguments[parameter] = ""
    with pytest.raises(ValueError):
        contentful_source(**arguments)
    assert fake.requests == []


@pytest.mark.parametrize("token", ["test\x00value", "test\x7fvalue"])
def test_token_control_characters_are_rejected_before_request(fake, token):
    with pytest.raises(ValueError):
        contentful_source(
            space_id=fake.space,
            token=token,
            resource_name="contentful_default",
            service=fake.client,
        )
    assert fake.requests == []


def test_explicit_credentials_rotate_without_changing_resource_or_checkpoint(pipeline, fake):
    first = contentful_source(
        space_id=fake.space, token="FIRST_TOKEN", resource_name="articles", service=fake.client
    )
    assert first.name == "articles"
    pipeline.run(first)
    second = contentful_source(
        space_id=fake.space, token="SECOND_TOKEN", resource_name="articles", service=fake.client
    )
    assert second.name == first.name
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
    arguments = {
        "space_id": fake.space,
        "token": "test",
        "resource_name": "contentful_default",
        "service": fake.client,
    }
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
        contentful_source(
            space_id=fake.space,
            token="secret",
            resource_name="contentful_default",
            host=host,
            service=fake.client,
        )
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


def test_rejected_sync_token_recovers_with_full_refresh_in_same_dataset(pipeline, fake):
    fake.initial_items = [fake.entry("one", "OLD_PAYLOAD"), fake.entry("removed", "REMOVED")]
    table = _run(pipeline, fake)
    fake.initial_items = [fake.entry("one", "REFRESHED_PAYLOAD"), fake.entry("new", "NEW")]
    fake.initial_token = "refreshed"
    fake.override = lambda request: (
        httpx.Response(400, json={"sys": {"id": "InvalidQuery"}, "message": "bad token"})
        if request.url.params.get("sync_token") == "t1"
        else None
    )
    before = len(_sync_requests(fake))
    assert _run(pipeline, fake) == table
    requests = _sync_requests(fake)[before:]
    assert requests[0].url.params["sync_token"] == "t1"
    assert requests[1].url.params.get("initial") in {"true", "True", "1"}
    text = _text(_rows(pipeline, table))
    assert "REFRESHED_PAYLOAD" in text and "NEW" in text
    assert "OLD_PAYLOAD" not in text and "REMOVED" not in text
    _run(pipeline, fake)
    assert _sync_requests(fake)[-1].url.params["sync_token"] == "refreshed"


def test_failed_token_refresh_preserves_rows_and_previous_checkpoint(pipeline, fake):
    fake.initial_items = [fake.entry("one", "RETAINED_PAYLOAD")]
    table = _run(pipeline, fake)
    before = _rows(pipeline, table)

    def fail_refresh(request):
        if request.url.params.get("sync_token") == "t1":
            return httpx.Response(400, json={"sys": {"id": "InvalidQuery"}})
        if request.url.params.get("initial"):
            return httpx.Response(401, json={"sys": {"id": "AccessTokenInvalid"}})
        return None

    fake.override = fail_refresh
    with pytest.raises(PipelineStepFailed):
        _run(pipeline, fake)
    assert _rows(pipeline, table) == before
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
    fake.models = [fake.model(fields=[{"id": "body", "type": "RichText"}])]
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
    assert "[paragraph]" not in content and "[bold]" not in content


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


@pytest.mark.parametrize(
    "fault",
    ["duplicate", "wrong_skip", "missing_items", "bad_total", "bad_limit", "short_page"],
)
def test_unreliable_model_inventory_never_deletes_previous_models(pipeline, fake, fault):
    fake.models = [fake.model()]
    table = _run(pipeline, fake)
    before = _rows(pipeline, table)

    def override(request):
        if not request.url.path.endswith("/content_types"):
            return None
        payload = {"sys": {"type": "Array"}, "items": [], "total": 0, "skip": 0, "limit": 100}
        if fault == "duplicate":
            payload.update(items=[fake.model(), fake.model()], total=2)
        elif fault == "wrong_skip":
            payload["skip"] = 1
        elif fault == "missing_items":
            del payload["items"]
        elif fault == "bad_total":
            payload["total"] = -1
        elif fault == "bad_limit":
            payload["limit"] = 0
        else:
            payload.update(total=2, limit=1)
        return httpx.Response(200, json=payload)

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


@pytest.mark.parametrize("status", [401, 503])
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


@pytest.mark.parametrize("resolution", ["wrong_id", "wrong_environment", "failure"])
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


def test_stale_not_found_keeps_newer_republished_sync_event(pipeline, fake):
    fake.initial_items = [fake.entry("one", "RETAINED_PAYLOAD")]
    table = _run(pipeline, fake)
    fake.delta(
        "t1",
        [
            fake.deleted("one", deleted_at="2026-02-01T00:00:00Z"),
            fake.entry("one", "REPUBLISHED_PAYLOAD", updated_at="2026-03-01T00:00:00Z"),
        ],
        "t2",
    )
    fake.live[("Entry", "one")] = None
    _run(pipeline, fake)
    assert "REPUBLISHED_PAYLOAD" in _text(_rows(pipeline, table))
    assert len(_lookup_requests(fake, "entries")) == 2
    _run(pipeline, fake)
    assert _sync_requests(fake)[-1].url.params["sync_token"] == "t2"


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
        assert sum(waits) == 2


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
    first_table = first.name
    pipeline.run(first)
    second = _source(
        fake, content_type_ids=["article", "product", "article"], locales=["fr-FR", "en-US"]
    )
    assert second.name == first_table
    pipeline.run(second)
    assert _sync_requests(fake)[-1].url.params.get("sync_token") == "t1"


def test_factory_arguments_are_keyword_only_and_do_not_read_environment(
    pipeline, fake, monkeypatch
):
    import inspect

    signature = inspect.signature(contentful_source)
    assert all(
        argument.kind is inspect.Parameter.KEYWORD_ONLY
        for argument in signature.parameters.values()
    )
    for name in ("space_id", "token", "resource_name"):
        assert signature.parameters[name].default is inspect.Parameter.empty
    monkeypatch.setenv("CONTENTFUL_SPACE_ID", fake.space)
    monkeypatch.setenv("CONTENTFUL_DELIVERY_TOKEN", "ENV_TOKEN")
    monkeypatch.setenv("CONTENTFUL_ENVIRONMENT_ID", "from_environment")
    with pytest.raises(TypeError):
        contentful_source(service=fake.client)
    with pytest.raises(ValueError):
        contentful_source(space_id=None, token=None, resource_name="articles", service=fake.client)
    resource = contentful_source(
        space_id=fake.space, token="EXPLICIT_TOKEN", resource_name="articles", service=fake.client
    )
    list(resource)
    assert all("/environments/master/" in request.url.path for request in fake.requests)
    assert all(
        request.headers["authorization"] == "Bearer EXPLICIT_TOKEN" for request in fake.requests
    )


def test_hosted_resource_has_document_contract_and_integer_statistics(pipeline, fake):
    from cognee.tasks.ingestion import dlt_utils
    from dlt.extract.resource import DltResource

    fake.initial_items = [fake.entry("one", "ONE"), fake.entry("one", "ONE")]
    resource = _source(fake, resource_name="articles")
    assert isinstance(resource, DltResource)
    assert resource.name == "articles"
    assert getattr(resource, dlt_utils.DOCUMENT_SOURCE_ATTR) == "contentful"
    assert getattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR)
    assert resource.cognee_sync_stats == {
        "synced": 0,
        "deleted": 0,
        "failed_rate_limit": 0,
        "failed_auth": 0,
    }
    pipeline.run(resource)
    assert resource.cognee_sync_stats == {
        "synced": 1,
        "deleted": 0,
        "failed_rate_limit": 0,
        "failed_auth": 0,
    }
    fake.delta("t1", [fake.deleted("one"), fake.deleted("one")], "t2")
    pipeline.run(resource)
    assert resource.cognee_sync_stats == {
        "synced": 0,
        "deleted": 1,
        "failed_rate_limit": 0,
        "failed_auth": 0,
    }
    pipeline.run(resource)
    assert resource.cognee_sync_stats == {
        "synced": 0,
        "deleted": 0,
        "failed_rate_limit": 0,
        "failed_auth": 0,
    }
    assert all(type(count) is int for count in resource.cognee_sync_stats.values())


@pytest.mark.parametrize("change", ["source_id", "environment", "space_id", "host"])
def test_resource_name_cannot_adopt_another_source_ownership(pipeline, fake, change):
    fake.initial_items = [fake.entry("one", "RETAINED")]
    table = _run(pipeline, fake, resource_name="articles")
    before = _rows(pipeline, table)
    arguments = {
        "space_id": fake.space,
        "environment": fake.environment,
        "host": fake.host,
        "source_id": "default",
        "token": "SECRET_DELIVERY_TOKEN",
        "resource_name": "articles",
        "service": fake.client,
    }
    arguments[change] = "cdn.eu.contentful.com" if change == "host" else "different"
    before_requests = len(fake.requests)
    with pytest.raises(PipelineStepFailed):
        pipeline.run(contentful_source(**arguments))
    assert len(fake.requests) == before_requests
    assert _rows(pipeline, table) == before


def test_content_types_use_offset_pages_with_real_delivery_response_shape(pipeline, fake):
    fake.models = [fake.model("article"), fake.model("product"), fake.model("news")]
    fake.model_page_size = 2
    table = _run(pipeline, fake)
    assert len(_rows(pipeline, table)) == 3
    requests = [request for request in fake.requests if request.url.path.endswith("/content_types")]
    assert [int(request.url.params["skip"]) for request in requests] == [0, 2, 0, 2]
    assert all(int(request.url.params["limit"]) > 0 for request in requests)
    assert all(
        "cursor" not in request.url.params and "pageNext" not in request.url.params
        for request in requests
    )


def test_total_change_mid_inventory_aborts_without_deleting_previous_models(pipeline, fake):
    fake.models = [fake.model(), fake.model("product")]
    fake.model_page_size = 1
    table = _run(pipeline, fake)
    before = _rows(pipeline, table)

    def changing_total(request):
        if request.url.path.endswith("/content_types") and request.url.params.get("skip") == "1":
            return httpx.Response(
                200,
                json={
                    "sys": {"type": "Array"},
                    "items": [fake.model("product")],
                    "total": 3,
                    "skip": 1,
                    "limit": 1,
                },
            )
        return None

    fake.override = changing_total
    with pytest.raises(PipelineStepFailed):
        _run(pipeline, fake)
    assert _rows(pipeline, table) == before


def test_model_omitted_from_inventory_but_found_at_detail_is_retained(pipeline, fake):
    model = fake.model()
    fake.models = [model]
    table = _run(pipeline, fake)
    before = _rows(pipeline, table)
    fake.models = []
    fake.live[("ContentType", "article")] = model
    _run(pipeline, fake)
    assert _rows(pipeline, table) == before
    probes = _lookup_requests(fake, "content_types")
    assert 1 <= len(probes) <= 2


@pytest.mark.parametrize("kind", ["Entry", "Asset"])
@pytest.mark.parametrize("reverse", [False, True])
def test_stale_cdn_detail_retries_once_then_keeps_newest_sync_event(pipeline, fake, kind, reverse):
    constructor = fake.entry if kind == "Entry" else fake.asset
    old = constructor("same", "OLD", updated_at="2026-01-01T00:00:00Z")
    new = constructor("same", "CURRENT", updated_at="2026-03-01T00:00:00Z")
    events = [old, fake.deleted("same", kind=kind, deleted_at="2026-02-01T00:00:00Z"), new]
    if reverse:
        events.reverse()
    fake.initial_items = events
    fake.live[(kind, "same")] = old
    table = _run(pipeline, fake)
    assert "CURRENT" in _text(_rows(pipeline, table))
    assert "OLD" not in _text(_rows(pipeline, table))
    assert len(_lookup_requests(fake, "entries" if kind == "Entry" else "assets")) == 2


def test_conflict_with_equal_timestamps_and_no_authoritative_payload_aborts(pipeline, fake):
    fake.initial_items = [fake.entry("one", "FIRST"), fake.entry("one", "DIFFERENT")]
    fake.live[("Entry", "one")] = fake.entry("one", "STALE", updated_at="2025-01-01T00:00:00Z")
    with pytest.raises(PipelineStepFailed):
        _run(pipeline, fake)


def test_first_live_row_is_yielded_before_final_sync_page(pipeline, fake):
    fake.sync_pages["initial"] = {
        "items": [fake.entry("one", "FIRST")],
        "nextPageUrl": fake.sync_url("later"),
    }
    fake.delta("later", [fake.entry("two", "SECOND")], "terminal")
    iterator = iter(_source(fake))
    first = next(iterator)
    assert first["id"].endswith(":Entry:one")
    assert not first.get("_deleted", False)
    assert len(_sync_requests(fake)) == 1
    remaining = list(iterator)
    assert any(row["id"].endswith(":Entry:two") for row in remaining)
    assert len(_sync_requests(fake)) == 2


def test_tombstones_are_emitted_only_after_terminal_sync_page(pipeline, fake):
    fake.initial_items = [fake.entry("one", "ONE")]
    _run(pipeline, fake)
    fake.page("t1", [fake.deleted("one")], "later")
    fake.delta("later", [fake.entry("two", "TWO")], "terminal")
    before = len(_sync_requests(fake))
    resource = _source(fake)
    saw_tombstone = False
    for row in resource:
        if row.get("_deleted"):
            saw_tombstone = True
            assert len(_sync_requests(fake)) - before == 2
    assert saw_tombstone


def test_shared_httpx_logger_filters_and_records_remain_untouched(pipeline, fake):
    resource = _source(fake)  # Initialize Cognee's own logging before taking the baseline.
    shared_logger = logging.getLogger("httpx")
    messages = []

    class Capture(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage())

    capture = Capture()
    marker = logging.Filter()
    shared_logger.addFilter(marker)
    shared_logger.addHandler(capture)
    previous_level = shared_logger.level
    shared_logger.setLevel(logging.WARNING)
    original_filters = tuple(shared_logger.filters)

    def observe(request):
        assert tuple(shared_logger.filters) == original_filters
        shared_logger.warning("Unrelated URL access_token=other-project-token")
        return None

    fake.override = observe
    try:
        shared_logger.warning("Unrelated URL access_token=other-project-token")
        baseline = messages.pop()
        pipeline.run(resource)
        assert messages == [baseline] * len(fake.requests)
        assert tuple(shared_logger.filters) == original_filters
    finally:
        shared_logger.removeFilter(marker)
        shared_logger.removeHandler(capture)
        shared_logger.setLevel(previous_level)


def test_check_active_runs_before_every_request_including_retries_and_lookups(pipeline, fake):
    checks = []
    fake.initial_items = [
        fake.entry("one", "OLD"),
        fake.entry("one", "NEW", updated_at="2026-03-01T00:00:00Z"),
    ]
    fake.live[("Entry", "one")] = fake.initial_items[-1]
    rate_limited = False

    def check_active():
        checks.append(len(fake.requests))

    def override(request):
        nonlocal rate_limited
        assert len(checks) == len(fake.requests)
        if request.url.path.endswith("/sync") and not rate_limited:
            rate_limited = True
            return httpx.Response(429, headers={"X-Contentful-RateLimit-Reset": "0"})
        return None

    fake.override = override
    resource = _source(fake, check_active=check_active)
    pipeline.run(resource)
    assert checks == [*range(len(fake.requests)), len(fake.requests)]
    assert _lookup_requests(fake, "entries")
    assert resource.cognee_sync_stats["failed_rate_limit"] == 1


def test_cancellation_between_requests_does_not_advance_checkpoint(pipeline, fake):
    fake.initial_items = [fake.entry("one", "RETAINED")]
    table = _run(pipeline, fake)
    before = _rows(pipeline, table)
    fake.page("t1", [fake.entry("one", "UNCOMMITTED")], "later")
    fake.delta("later", [], "t2")
    initial_request_count = len(fake.requests)

    class CancelledError(Exception):
        pass

    def check_active():
        if any(
            request.url.params.get("sync_token") == "t1"
            for request in fake.requests[initial_request_count:]
        ):
            raise CancelledError("cancelled before next request")

    with pytest.raises(PipelineStepFailed) as captured:
        pipeline.run(_source(fake, check_active=check_active))
    assert "cancelled before next request" in str(captured.value)
    assert _rows(pipeline, table) == before
    assert all(
        request.url.params.get("sync_token") != "later"
        for request in fake.requests[initial_request_count:]
    )
    count = len(_sync_requests(fake))
    _run(pipeline, fake)
    assert _sync_requests(fake)[count].url.params["sync_token"] == "t1"


@pytest.mark.parametrize(
    "status,error_name,counter",
    [
        (401, "ContentfulAuthError", "failed_auth"),
        (403, "ContentfulAuthError", "failed_auth"),
        (429, "ContentfulRateLimitError", "failed_rate_limit"),
        (503, "ContentfulAPIError", None),
    ],
)
def test_exported_error_types_and_failure_statistics(pipeline, fake, status, error_name, counter):
    import cognee_community_connector_contentful as connector

    fake.override = lambda request: httpx.Response(status, text="SECRET_DELIVERY_TOKEN")
    resource = _source(fake)
    with pytest.raises(Exception) as captured:
        list(resource)
    error = captured.value
    chain = []
    while error is not None and error not in chain:
        chain.append(error)
        error = error.__cause__ or error.__context__
    assert any(isinstance(error, getattr(connector, error_name)) for error in chain)
    assert all("SECRET_DELIVERY_TOKEN" not in str(error) for error in chain)
    if counter:
        assert resource.cognee_sync_stats[counter] == len(fake.requests)


def test_display_field_title_locale_order_editor_url_and_plain_rich_text(pipeline, fake):
    model = fake.model()
    model["displayField"] = "headline"
    model["fields"][1]["type"] = "RichText"
    fake.models = [model]
    fake.initial_items = [
        fake.entry(
            "one",
            "",
            fields={
                "headline": {"en-US": "English headline", "fr-FR": "Titre français"},
                "body": {
                    "en-US": {
                        "nodeType": "document",
                        "data": {},
                        "content": [
                            {
                                "nodeType": "paragraph",
                                "data": {},
                                "content": [
                                    {
                                        "nodeType": "text",
                                        "value": "First paragraph.",
                                        "marks": [{"type": "bold"}],
                                        "data": {},
                                    }
                                ],
                            },
                            {
                                "nodeType": "paragraph",
                                "data": {},
                                "content": [
                                    {
                                        "nodeType": "text",
                                        "value": "Second paragraph.",
                                        "marks": [],
                                        "data": {},
                                    }
                                ],
                            },
                        ],
                    }
                },
            },
        )
    ]
    table = _run(pipeline, fake, locales=["fr-FR", "en-US"])
    row = next(row for row in _rows(pipeline, table).values() if row["id"].endswith(":Entry:one"))
    assert row["title"] == "Titre français"
    assert row["url"] == "https://app.contentful.com/spaces/space/environments/master/entries/one"
    assert row["content"].startswith("Titre français")
    assert "First paragraph.\n\nSecond paragraph." in row["content"]
    assert "[paragraph]" not in row["content"] and "[bold]" not in row["content"]
    assert row["content"].index("First paragraph.") < row["content"].index("Space:")
    _run(pipeline, fake, locales=["en-US", "fr-FR"])
    changed = _rows(pipeline, table)[row["id"]]
    assert changed["title"] == "English headline"


def test_model_display_field_change_refreshes_entry_title_without_delta(pipeline, fake):
    fake.models = [fake.model()]
    fake.initial_items = [
        fake.entry(
            "one", "", fields={"title": {"en-US": "Old title"}, "headline": {"en-US": "New title"}}
        )
    ]
    table = _run(pipeline, fake)
    before = _rows(pipeline, table)
    entry_id = next(identifier for identifier in before if identifier.endswith(":Entry:one"))
    assert before[entry_id]["title"] == "Old title"
    fake.models[0]["displayField"] = "headline"
    _run(pipeline, fake)
    assert _rows(pipeline, table)[entry_id]["title"] == "New title"


def test_shuffled_events_models_and_field_mappings_produce_identical_documents(pipeline, fake):
    first = fake.entry(
        "one",
        "",
        fields={
            "zeta": {"fr-FR": "FR", "en-US": "EN"},
            "alpha": {"en-US": "A"},
            "ordered": {"en-US": ["first", "second"]},
        },
    )
    second = fake.entry("two", "SECOND")
    models = [fake.model("article"), fake.model("product")]
    fake.initial_items = [first, second]
    fake.models = models
    table = _run(pipeline, fake, source_id="original")
    original = _rows(pipeline, table)
    shuffled = deepcopy(first)
    shuffled["fields"] = {
        key: dict(reversed(list(value.items())))
        for key, value in reversed(list(first["fields"].items()))
    }
    fake.initial_items = [second, shuffled]
    fake.models = list(reversed(models))
    for model in fake.models:
        model["fields"].reverse()
    other = _run(pipeline, fake, source_id="shuffled")
    assert _rows(pipeline, other) == original
    assert "first" in _text(original) and "second" in _text(original)


def test_token_rejection_after_provisional_rows_refreshes_without_leaking_them(pipeline, fake):
    fake.initial_items = [fake.entry("retained", "ORIGINAL"), fake.entry("removed", "REMOVED")]
    table = _run(pipeline, fake)
    fake.page("t1", [fake.entry("provisional", "MUST_NOT_SURVIVE")], "expired-page")
    fake.initial_items = [fake.entry("retained", "REFRESHED")]
    fake.initial_token = "recovered"
    fake.override = lambda request: (
        httpx.Response(400, json={"sys": {"id": "InvalidQuery"}})
        if request.url.params.get("sync_token") == "expired-page"
        else None
    )
    _run(pipeline, fake)
    rows = _rows(pipeline, table)
    assert len(rows) == 1
    assert "REFRESHED" in _text(rows)
    assert "REMOVED" not in _text(rows) and "MUST_NOT_SURVIVE" not in _text(rows)
    _run(pipeline, fake)
    assert _sync_requests(fake)[-1].url.params["sync_token"] == "recovered"


def test_second_rejected_token_during_refresh_aborts_without_endless_recovery(pipeline, fake):
    fake.initial_items = [fake.entry("one", "RETAINED")]
    table = _run(pipeline, fake)
    before = _rows(pipeline, table)
    fake.sync_pages["initial"] = {
        "items": [fake.entry("one", "UNCOMMITTED")],
        "nextPageUrl": fake.sync_url("expired-refresh-page"),
    }
    fake.override = lambda request: (
        httpx.Response(400, json={"sys": {"id": "InvalidQuery"}})
        if request.url.params.get("sync_token") in {"t1", "expired-refresh-page"}
        else None
    )
    requests_before = len(_sync_requests(fake))
    with pytest.raises(PipelineStepFailed):
        _run(pipeline, fake)
    assert _rows(pipeline, table) == before
    assert len(_sync_requests(fake)) - requests_before == 3


def test_error_hierarchy_is_exported_with_distinct_response_and_token_errors(pipeline, fake):
    import cognee_community_connector_contentful as connector

    assert issubclass(connector.ContentfulAPIError, connector.ContentfulSourceError)
    assert issubclass(connector.ContentfulAuthError, connector.ContentfulAPIError)
    assert issubclass(connector.ContentfulRateLimitError, connector.ContentfulAPIError)
    assert issubclass(connector.ContentfulSyncTokenError, connector.ContentfulAPIError)
    assert issubclass(connector.ContentfulResponseError, connector.ContentfulSourceError)
    fake.sync_pages["initial"] = {"items": []}
    resource = _source(fake)
    with pytest.raises(Exception) as captured:
        list(resource)
    error = captured.value
    while error is not None and not isinstance(error, connector.ContentfulResponseError):
        error = error.__cause__ or error.__context__
    assert isinstance(error, connector.ContentfulResponseError)


def test_display_field_absent_or_non_text_falls_back_to_entry_id(pipeline, fake):
    fake.models = [fake.model()]
    fake.initial_items = [
        fake.entry("missing", "", fields={"other": {"en-US": "Not the display field"}}),
        fake.entry("number", "", fields={"title": {"en-US": 42}}),
    ]
    table = _run(pipeline, fake)
    entries = [row for row in _rows(pipeline, table).values() if ":Entry:" in row["id"]]
    assert {row["title"] for row in entries} == {"missing", "number"}


def test_default_display_locale_selection_is_stable_across_mapping_order(pipeline, fake):
    fake.models = [fake.model()]
    fake.initial_items = [
        fake.entry("one", "", fields={"title": {"fr-FR": "French", "en-US": "English"}})
    ]
    table = _run(pipeline, fake, source_id="first")
    first = _rows(pipeline, table)
    fake.initial_items[0]["fields"]["title"] = {"en-US": "English", "fr-FR": "French"}
    other = _run(pipeline, fake, source_id="second")
    assert _rows(pipeline, other) == first
    assert next(row for row in first.values() if ":Entry:" in row["id"])["title"] == "English"


def test_example_runs_two_foreground_syncs_then_searches_with_mocked_provider(
    pipeline, fake, monkeypatch, capsys
):
    import asyncio
    import runpy
    from pathlib import Path

    import cognee

    import cognee_community_connector_contentful as connector

    fake.initial_items = [fake.entry("one", "FIRST"), fake.entry("removed", "REMOVED")]
    fake.delta("t1", [fake.entry("one", "SECOND"), fake.deleted("removed")], "t2")
    monkeypatch.setenv("CONTENTFUL_SPACE_ID", fake.space)
    monkeypatch.setenv("CONTENTFUL_DELIVERY_TOKEN", "offline-example-token")
    monkeypatch.setenv("CONTENTFUL_ENVIRONMENT_ID", fake.environment)
    monkeypatch.setenv("CONTENTFUL_RESOURCE_NAME", "example_documents")
    monkeypatch.setenv("CONTENTFUL_DATASET", "example_dataset")
    monkeypatch.setenv("CONTENTFUL_QUERY", "Summarize the second sync")
    for name in (
        "CONTENTFUL_CONTENT_TYPE_IDS",
        "CONTENTFUL_LOCALES",
        "CONTENTFUL_INCLUDE_ASSETS",
        "CONTENTFUL_SOURCE_ID",
        "CONTENTFUL_DELIVERY_HOST",
    ):
        monkeypatch.delenv(name, raising=False)
    resources = []
    calls = []

    def source_with_mocked_service(**kwargs):
        resource = contentful_source(**kwargs, service=fake.client)
        resources.append(resource)
        return resource

    async def remember(resource, **kwargs):
        assert kwargs["dataset_name"] == "example_dataset"
        assert kwargs["primary_key"] == "id"
        assert kwargs["write_disposition"] == "merge"
        assert kwargs["run_in_background"] is False
        assert kwargs["max_rows_per_table"] == 0
        pipeline.run(resource).raise_on_failed_jobs()
        calls.append("remember")

    async def search(**kwargs):
        assert calls == ["remember", "remember"]
        assert kwargs["query_text"] == "Summarize the second sync"
        assert kwargs["datasets"] == ["example_dataset"]
        calls.append("search")
        return ["Mocked model answer"]

    monkeypatch.setattr(connector, "contentful_source", source_with_mocked_service)
    monkeypatch.setattr(cognee, "remember", remember)
    monkeypatch.setattr(cognee, "search", search)
    example = Path(__file__).resolve().parents[1] / "examples" / "example.py"
    namespace = runpy.run_path(str(example), run_name="contentful_example_test")
    asyncio.run(namespace["main"]())
    assert calls == ["remember", "remember", "search"]
    assert len(resources) == 2 and resources[0].name == resources[1].name
    rows = _rows(pipeline, "example_documents")
    assert len(rows) == 1 and "SECOND" in _text(rows) and "REMOVED" not in _text(rows)
    output = capsys.readouterr().out
    assert "Sync 1/2" in output and "Sync 2/2" in output
    assert output.count("Extraction statistics:") == 2
    assert "Mocked model answer" in output


def test_failed_refresh_after_provisional_rows_preserves_staging_and_checkpoint(pipeline, fake):
    fake.initial_items = [fake.entry("one", "RETAINED")]
    table = _run(pipeline, fake)
    before = _rows(pipeline, table)
    fake.page("t1", [fake.entry("provisional", "UNCOMMITTED")], "expired")

    def fail_after_provisional(request):
        if request.url.params.get("sync_token") == "expired":
            return httpx.Response(400, json={"sys": {"id": "InvalidQuery"}})
        if request.url.params.get("initial"):
            return httpx.Response(401, json={"sys": {"id": "AccessTokenInvalid"}})
        return None

    fake.override = fail_after_provisional
    with pytest.raises(PipelineStepFailed):
        _run(pipeline, fake)
    assert _rows(pipeline, table) == before
    fake.override = None
    fake.delta("expired", [], "t2")
    requests_before = len(_sync_requests(fake))
    _run(pipeline, fake)
    assert _sync_requests(fake)[requests_before].url.params["sync_token"] == "t1"
    assert "UNCOMMITTED" in _text(_rows(pipeline, table))


@pytest.mark.parametrize("change", ["upsert", "dedup_sort", "table_name"])
def test_merge_dedup_and_resource_table_hints_are_required_before_requests(pipeline, fake, change):
    resource = _source(fake)
    if change == "upsert":
        resource.apply_hints(write_disposition={"disposition": "merge", "strategy": "upsert"})
    elif change == "dedup_sort":
        resource.apply_hints(columns={"contentful_dlt_priority": {"dedup_sort": "asc"}})
    else:
        resource.apply_hints(table_name="another_table")
    with pytest.raises(PipelineStepFailed):
        pipeline.run(resource)
    assert fake.requests == []


def test_shuffled_conflicting_pages_converge_to_identical_final_documents(pipeline, fake):
    old = fake.entry("same", "OLD", updated_at="2026-01-01T00:00:00Z")
    new = fake.entry("same", "CURRENT", updated_at="2026-03-01T00:00:00Z")
    deleted = fake.deleted("same", deleted_at="2026-02-01T00:00:00Z")
    fake.live[("Entry", "same")] = new
    fake.sync_pages["initial"] = {
        "items": [old, deepcopy(old)],
        "nextPageUrl": fake.sync_url("page2"),
    }
    fake.delta("page2", [deleted, new], "terminal")
    table = _run(pipeline, fake, source_id="first")
    before = _rows(pipeline, table)
    fake.sync_pages["initial"] = {"items": [new], "nextPageUrl": fake.sync_url("page2")}
    fake.delta("page2", [deleted, old, deepcopy(new)], "terminal")
    second = _run(pipeline, fake, source_id="second")
    assert _rows(pipeline, second) == before
    assert len(before) == 1 and "CURRENT" in _text(before) and "OLD" not in _text(before)


@pytest.mark.parametrize("reverse", [False, True])
def test_equal_timestamp_conflicts_accept_authoritative_lookup_in_either_order(
    pipeline, fake, reverse
):
    from cognee_community_connector_contentful._http import fingerprint

    candidates = [fake.entry("same", "FIRST_PAYLOAD"), fake.entry("same", "SECOND_PAYLOAD")]
    # The matching detail must be the second fingerprint, guarding against
    # selecting one arbitrary equal-timestamp event before consulting the CDN.
    authoritative = sorted(candidates, key=fingerprint)[1]
    expected = authoritative["fields"]["body"]["en-US"]
    fake.live[("Entry", "same")] = authoritative
    fake.initial_items = list(reversed(candidates)) if reverse else candidates
    table = _run(pipeline, fake)
    rows = _rows(pipeline, table)
    assert len(rows) == 1
    assert expected in _text(rows)
    assert len(_lookup_requests(fake, "entries")) == 1


@pytest.mark.parametrize("invalid_type", [[], {}])
@pytest.mark.parametrize("endpoint", ["sync", "models"])
def test_malformed_entity_type_has_named_response_error_not_type_error(
    pipeline, fake, invalid_type, endpoint
):
    from cognee_community_connector_contentful import ContentfulResponseError

    entity = fake.entry("one", "INVALID") if endpoint == "sync" else fake.model()
    entity["sys"]["type"] = invalid_type
    if endpoint == "sync":
        fake.initial_items = [entity]
    else:
        fake.models = [entity]
    with pytest.raises(PipelineStepFailed) as captured:
        _run(pipeline, fake)
    error = captured.value
    chain = []
    while error is not None and error not in chain:
        chain.append(error)
        error = error.__cause__ or error.__context__
    assert any(isinstance(error, ContentfulResponseError) for error in chain)
    assert not any(isinstance(error, TypeError) for error in chain)


def test_retained_model_preserves_title_policy_until_complete_inventory_confirms_change(
    pipeline, fake
):
    original_model = fake.model()
    fake.models = [original_model]
    one = fake.entry(
        "one",
        "",
        fields={
            "title": {"en-US": "ONE_OLD_TITLE"},
            "headline": {"en-US": "ONE_NEW_TITLE"},
            "body": {"en-US": "ONE_ORIGINAL_BODY"},
        },
    )
    two = fake.entry(
        "two",
        "",
        fields={
            "title": {"en-US": "TWO_OLD_TITLE"},
            "headline": {"en-US": "TWO_NEW_TITLE"},
            "body": {"en-US": "TWO_BODY"},
        },
    )
    fake.initial_items = [one, two]
    table = _run(pipeline, fake)
    before = _rows(pipeline, table)
    model_id = next(identifier for identifier in before if ":ContentType:" in identifier)
    one_id = next(identifier for identifier in before if identifier.endswith(":Entry:one"))
    two_id = next(identifier for identifier in before if identifier.endswith(":Entry:two"))
    assert before[one_id]["title"] == "ONE_OLD_TITLE"
    assert before[two_id]["title"] == "TWO_OLD_TITLE"

    changed_model = deepcopy(original_model)
    changed_model["displayField"] = "headline"
    changed_model["sys"]["revision"] = 2
    fake.models = []
    fake.live[("ContentType", "article")] = changed_model
    updated_one = deepcopy(one)
    updated_one["fields"]["body"]["en-US"] = "ONE_UPDATED_BODY"
    fake.delta("t1", [updated_one], "t2")
    _run(pipeline, fake)
    retained = _rows(pipeline, table)
    assert retained[model_id] == before[model_id]
    assert retained[one_id]["title"] == "ONE_OLD_TITLE"
    assert retained[two_id]["title"] == "TWO_OLD_TITLE"
    assert "ONE_UPDATED_BODY" in retained[one_id]["content"]
    assert "ONE_ORIGINAL_BODY" not in retained[one_id]["content"]
    assert retained[two_id] == before[two_id]
    assert _sync_requests(fake)[-1].url.params["sync_token"] == "t1"

    fake.models = [changed_model]
    fake.initial_items = [updated_one, two]
    fake.initial_token = "confirmed"
    sync_count = len(_sync_requests(fake))
    _run(pipeline, fake)
    confirmed = _rows(pipeline, table)
    assert _sync_requests(fake)[sync_count].url.params.get("initial") in {"true", "True", "1"}
    assert confirmed[one_id]["title"] == "ONE_NEW_TITLE"
    assert confirmed[two_id]["title"] == "TWO_NEW_TITLE"
    assert confirmed[model_id] != before[model_id]
    _run(pipeline, fake)
    assert _sync_requests(fake)[-1].url.params["sync_token"] == "confirmed"


@pytest.mark.parametrize("hint", ["pre_deduplicated", "disabled_delete", "text_delete"])
def test_streaming_merge_rejects_hints_that_bypass_reconciliation(pipeline, fake, hint):
    resource = _source(fake)
    if hint == "pre_deduplicated":
        resource.apply_hints(
            write_disposition={
                "disposition": "merge",
                "strategy": "delete-insert",
                "deduplicated": True,
            }
        )
    else:
        resource.apply_hints(
            columns={
                "_deleted": {
                    "data_type": "text" if hint == "text_delete" else "bool",
                    "hard_delete": hint == "text_delete",
                }
            }
        )
    with pytest.raises(PipelineStepFailed):
        pipeline.run(resource)
    assert fake.requests == []


def test_new_empty_model_keeps_delta_and_does_not_reemit_existing_entries(pipeline, fake):
    fake.models = [fake.model()]
    fake.initial_items = [fake.entry(str(index), "UNCHANGED") for index in range(500)]
    _run(pipeline, fake)
    fake.models.append(fake.model("new_type"))
    fake.delta("t1", [], "t2")
    before_requests = len(_sync_requests(fake))
    resource = _source(fake)
    rows = []
    resource.add_map(lambda row: rows.append(row.copy()) or row)
    pipeline.run(resource).raise_on_failed_jobs()
    assert [row["id"].rsplit(":", 1)[-1] for row in rows] == ["new_type"]
    requests = _sync_requests(fake)[before_requests:]
    assert len(requests) == 1 and requests[0].url.params.get("sync_token") == "t1"


def test_only_changed_entry_is_rendered_on_delta(pipeline, fake, monkeypatch):
    import importlib

    module = importlib.import_module("cognee_community_connector_contentful.contentful")
    fake.models = [fake.model()]
    fake.initial_items = [fake.entry("one", "OLD"), fake.entry("two", "UNCHANGED")]
    table = _run(pipeline, fake)
    real_document = module.document
    rendered = []

    def observe(item, *args, **kwargs):
        rendered.append((item["sys"]["type"], item["sys"]["id"]))
        return real_document(item, *args, **kwargs)

    monkeypatch.setattr(module, "document", observe)
    fake.delta("t1", [fake.entry("one", "CHANGED")], "t2")
    _run(pipeline, fake)
    assert rendered == [("Entry", "one")]
    assert "CHANGED" in _text(_rows(pipeline, table))


def test_published_then_deleted_between_runs_clears_provisional_row_without_deleted_stat(
    pipeline, fake
):
    table = _run(pipeline, fake)
    fake.page("t1", [fake.entry("ephemeral", "MUST_DISAPPEAR")], "second")
    fake.delta("second", [fake.deleted("ephemeral")], "t2")
    resource = _source(fake)
    observed = []
    resource.add_map(lambda row: observed.append(row.copy()) or row)
    pipeline.run(resource).raise_on_failed_jobs()
    assert any(row.get("_deleted") for row in observed)
    assert _rows(pipeline, table) == {}
    assert resource.cognee_sync_stats["synced"] == 0
    assert resource.cognee_sync_stats["deleted"] == 0


@pytest.mark.parametrize("status", [400, 410])
def test_recovery_same_id_overrides_partially_streamed_delta(pipeline, fake, status):
    fake.initial_items = [fake.entry("same", "ORIGINAL")]
    table = _run(pipeline, fake)
    fake.page("t1", [fake.entry("same", "PROVISIONAL")], "rejected")
    fake.initial_items = [fake.entry("same", "AUTHORITATIVE_REFRESH")]
    fake.initial_token = "recovered"
    fake.override = lambda request: (
        httpx.Response(status, json={"sys": {"id": "InvalidQuery"}})
        if request.url.params.get("sync_token") == "rejected"
        else None
    )
    resource = _source(fake)
    emitted = []
    resource.add_map(lambda row: emitted.append(row.copy()) or row)
    pipeline.run(resource).raise_on_failed_jobs()
    text = _text(_rows(pipeline, table))
    assert "AUTHORITATIVE_REFRESH" in text and "PROVISIONAL" not in text
    priorities = {row["content"]: row["contentful_dlt_priority"] for row in emitted}
    provisional = next(value for key, value in priorities.items() if "PROVISIONAL" in key)
    refreshed = next(value for key, value in priorities.items() if "AUTHORITATIVE_REFRESH" in key)
    assert refreshed > provisional


@pytest.mark.parametrize("status", [400, 410])
def test_initial_sync_continuation_rejection_never_triggers_full_refresh(pipeline, fake, status):
    from cognee_community_connector_contentful import ContentfulSyncTokenError

    fake.sync_pages["initial"] = {
        "items": [fake.entry("one", "UNCOMMITTED")],
        "nextPageUrl": fake.sync_url("broken-initial-page"),
    }
    fake.override = lambda request: (
        httpx.Response(status, json={"sys": {"id": "InvalidQuery"}})
        if request.url.params.get("sync_token") == "broken-initial-page"
        else None
    )
    with pytest.raises(PipelineStepFailed) as captured:
        _run(pipeline, fake)
    assert len(_sync_requests(fake)) == 2
    error = captured.value
    while error is not None:
        assert not isinstance(error, ContentfulSyncTokenError)
        error = error.__cause__ or error.__context__
    # The incomplete initial load must not advance state.
    fake.override = None
    fake.sync_pages.clear()
    before = len(_sync_requests(fake))
    _run(pipeline, fake)
    assert _sync_requests(fake)[before].url.params.get("initial") == "true"


def test_locale_selection_keeps_single_locale_fields_and_asset_file(pipeline, fake):
    model = fake.model(
        fields=[
            {"id": "title", "type": "Symbol", "localized": False},
            {"id": "body", "type": "Text", "localized": True},
            {"id": "slug", "type": "Symbol", "localized": False},
            {"id": "count", "type": "Integer", "localized": False},
            {"id": "reference", "type": "Link", "localized": False},
        ]
    )
    fake.models = [model]
    fake.initial_items = [
        fake.entry(
            "one",
            "",
            fields={
                "title": {"en-US": "Real Title"},
                "slug": {"en-US": "real-slug"},
                "count": {"en-US": 7},
                "reference": {"en-US": {"sys": {"type": "Link", "linkType": "Entry", "id": "REF"}}},
                "body": {"de-DE": "Deutsch", "en-US": "English"},
            },
        ),
        fake.asset("image", "ASSET_METADATA"),
    ]
    table = _run(pipeline, fake, locales=["de-DE"])
    rows = _rows(pipeline, table)
    entry = next(row for row in rows.values() if ":Entry:" in row["id"])
    asset = next(row for row in rows.values() if ":Asset:" in row["id"])
    assert entry["title"] == "Real Title"
    assert (
        "real-slug" in entry["content"]
        and "7" in entry["content"]
        and "Entry: REF" in entry["content"]
    )
    assert "body [de-DE]" in entry["content"] and "English" not in entry["content"]
    assert "slug [en-US]" in entry["content"]
    assert asset["url"] == "https://images.ctfassets.net/space/image/image.png"
    assert "image/png" in asset["content"] and "640" in asset["content"]


def test_default_locale_precedes_alphabetical_order_and_uses_environment_endpoint(pipeline, fake):
    fake.models = [fake.model()]
    fake.locale_page_size = 1
    fake.initial_items = [
        fake.entry(
            "one",
            "",
            fields={
                "title": {"de-DE": "Deutsch", "en-US": "English"},
            },
        )
    ]
    table = _run(pipeline, fake)
    row = next(row for row in _rows(pipeline, table).values() if ":Entry:" in row["id"])
    assert row["title"] == "English"
    requests = [request for request in fake.requests if request.url.path.endswith("/locales")]
    assert [request.url.params["skip"] for request in requests] == ["0", "1"]
    assert all("/environments/master/locales" in request.url.path for request in requests)
    fake.initial_items[0]["fields"]["title"] = {"en-US": "English", "de-DE": "Deutsch"}
    other = _run(pipeline, fake, source_id="reordered")
    assert _rows(pipeline, other) == _rows(pipeline, table)


@pytest.mark.parametrize(
    "value", [{"nodeType": 5}, {"nodeType": "x", "value": "v", "secret": "KEPT"}]
)
def test_object_field_with_node_type_is_preserved_json(pipeline, fake, value):
    fake.models = [fake.model(fields=[{"id": "object", "type": "Object"}])]
    fake.initial_items = [fake.entry("one", "", fields={"object": {"en-US": value}})]
    table = _run(pipeline, fake)
    import json

    assert json.dumps(value, sort_keys=True, separators=(",", ":")) in _text(_rows(pipeline, table))


def test_malformed_rich_text_keeps_raw_value_and_other_entities(pipeline, fake):
    fake.models = [fake.model(fields=[{"id": "body", "type": "RichText"}])]
    fake.initial_items = [
        fake.entry("broken", "", fields={"body": {"en-US": {"nodeType": 5, "value": "KEPT"}}}),
        fake.entry("good", "OTHER_ENTITY"),
    ]
    table = _run(pipeline, fake)
    text = _text(_rows(pipeline, table))
    assert "KEPT" in text and "OTHER_ENTITY" in text
    assert "Invalid RichText" in text


@pytest.mark.parametrize(
    "host,editor",
    [
        ("cdn.contentful.com", "app.contentful.com"),
        ("cdn.eu.contentful.com", "app.eu.contentful.com"),
    ],
)
def test_editor_url_matches_contentful_region(pipeline, host, editor):
    fake = FakeContentful(host=host, environment="staging")
    fake.initial_items = [fake.entry("one", "CONTENT")]
    try:
        table = _run(pipeline, fake)
        assert next(iter(_rows(pipeline, table).values()))["url"] == (
            f"https://{editor}/spaces/space/environments/staging/entries/one"
        )
    finally:
        fake.client.close()


def _node(kind, *children, **kwargs):
    return {"nodeType": kind, "content": list(children), "data": {}, **kwargs}


def _paragraph(text):
    return _node("paragraph", _node("text", value=text))


def test_rich_text_keeps_inline_spacing_nested_lists_and_multiline_cells(pipeline, fake):
    fake.models = [fake.model(fields=[{"id": "body", "type": "RichText"}])]
    rich = _node(
        "document",
        _node(
            "paragraph",
            _node("text", value="x"),
            _node(
                "embedded-entry-inline",
                data={"target": {"sys": {"type": "Link", "linkType": "Entry", "id": "E6"}}},
            ),
            _node("text", value="y"),
        ),
        _node(
            "unordered-list",
            _node(
                "list-item",
                _paragraph("a"),
                _node("unordered-list", _node("list-item", _paragraph("a1"))),
            ),
            _node("list-item", _paragraph("one"), _paragraph("two")),
        ),
        _node(
            "table",
            _node(
                "table-row",
                _node("table-cell", _paragraph("c1"), _paragraph("c1b")),
                _node("table-cell", _paragraph("c2")),
            ),
        ),
    )
    fake.initial_items = [fake.entry("one", "", fields={"body": {"en-US": rich}})]
    table = _run(pipeline, fake)
    text = _text(_rows(pipeline, table))
    assert "x Entry: E6 y" in text
    assert "- a\n  - a1" in text
    assert "- one\n\n  two" in text
    assert "c1 / c1b | c2" in text


@pytest.mark.parametrize("status", [429, 503, "transport"])
def test_cancellation_interrupts_retry_backoff_without_next_request(fake, monkeypatch, status):
    import cognee_community_connector_contentful._http as contentful_http
    from cognee_community_connector_contentful._http import DeliveryClient

    class CancelledError(RuntimeError):
        pass

    sleeps = []
    cancelled = False

    def check_active():
        if cancelled:
            raise CancelledError("cancelled during wait")

    def sleep(delay):
        nonlocal cancelled
        sleeps.append(delay)
        cancelled = True

    def override(request):
        if status == "transport":
            raise httpx.ConnectError("unavailable", request=request)
        return httpx.Response(status, headers={"X-Contentful-RateLimit-Reset": "60"})

    fake.override = override
    monkeypatch.setattr(contentful_http.time, "sleep", sleep)
    with (
        DeliveryClient(
            fake.host, fake.space, fake.environment, "token", fake.client, check_active
        ) as delivery,
        pytest.raises(CancelledError),
    ):
        delivery.get("sync", {"initial": "true"})
    assert len(fake.requests) == 1
    assert sleeps and max(sleeps) <= 0.25


@pytest.mark.parametrize("reset", ["nan", "inf", "-inf", "not-a-number"])
def test_nonfinite_retry_reset_uses_bounded_default(fake, monkeypatch, reset):
    import cognee_community_connector_contentful._http as contentful_http
    from cognee_community_connector_contentful._http import DeliveryClient

    sleeps = []
    fake.override = lambda request: (
        httpx.Response(429, headers={"X-Contentful-RateLimit-Reset": reset})
        if len(fake.requests) == 1
        else None
    )
    monkeypatch.setattr(contentful_http.time, "sleep", sleeps.append)
    with DeliveryClient(fake.host, fake.space, fake.environment, "token", fake.client) as delivery:
        delivery.get("sync", {"initial": "true"})
    assert sum(sleeps) == 1.0
    assert max(sleeps) <= 0.25


@pytest.mark.parametrize(
    "fault",
    ["no_default", "two_defaults", "duplicate", "missing_page", "changed_total", "wrong_scope"],
)
def test_invalid_locale_inventory_preserves_previous_staging_and_checkpoint(pipeline, fake, fault):
    fake.initial_items = [fake.entry("retained", "RETAINED")]
    table = _run(pipeline, fake)
    before = _rows(pipeline, table)
    fake.locale_page_size = 1
    if fault == "no_default":
        fake.locales[0]["default"] = False
    elif fault == "two_defaults":
        fake.locales[1]["default"] = True
    elif fault == "duplicate":
        fake.locales[1]["code"] = "en-US"
    elif fault == "wrong_scope":
        fake.locales[0]["sys"]["environment"] = {"sys": {"id": "other"}}
    else:

        def override(request):
            if request.url.path.endswith("/locales") and request.url.params.get("skip") == "1":
                return httpx.Response(
                    200,
                    json={
                        "items": [] if fault == "missing_page" else [fake.locales[1]],
                        "skip": 1,
                        "limit": 1,
                        "total": 3 if fault == "changed_total" else 2,
                    },
                )
            return None

        fake.override = override
    requests_before = len(_sync_requests(fake))
    with pytest.raises(PipelineStepFailed):
        _run(pipeline, fake)
    assert _rows(pipeline, table) == before
    assert len(_sync_requests(fake)) == requests_before
    fake.override = None
    fake.locales = [
        {"sys": {"id": "english", "type": "Locale"}, "code": "en-US", "default": True},
        {"sys": {"id": "german", "type": "Locale"}, "code": "de-DE", "default": False},
    ]
    _run(pipeline, fake)
    assert _sync_requests(fake)[-1].url.params["sync_token"] == "t1"


def test_default_locale_change_refreshes_existing_titles(pipeline, fake):
    fake.models = [fake.model()]
    fake.initial_items = [
        fake.entry(
            "one",
            "",
            fields={
                "title": {"de-DE": "Deutsch", "en-US": "English"},
            },
        )
    ]
    table = _run(pipeline, fake)
    fake.locales[0]["default"] = False
    fake.locales[1]["default"] = True
    _run(pipeline, fake)
    assert _sync_requests(fake)[-1].url.params["initial"] == "true"
    assert (
        next(row for row in _rows(pipeline, table).values() if ":Entry:" in row["id"])["title"]
        == "Deutsch"
    )


@pytest.mark.parametrize("system,raw", [("Darwin", 348 * 1024 * 1024), ("Linux", 348 * 1024)])
def test_memory_benchmark_rss_units_are_platform_specific(system, raw):
    from benchmark_contentful_memory import rss_mib

    assert rss_mib(raw, system) == 348
