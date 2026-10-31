"""Unit and dlt-pipeline tests for the Storyblok connector. No live space needed.

* Client tests run ``StoryblokClient`` against ``httpx.MockTransport``.
* Rendering tests cover how story blocks and rich text become text.
* Sync tests drive ``_iter_rows`` with a fake API and a plain dict as state, and
  ``storyblok_source`` runs through a dlt pipeline into a temp sqlite destination.
"""

import json
import logging

import httpx
import pytest
from conftest import SPACE_ID, FakeStoryblok

from cognee_community_connector_storyblok import storyblok as module
from cognee_community_connector_storyblok.storyblok import (
    REGION_HOSTS,
    StoryblokAPIError,
    StoryblokAuthError,
    StoryblokClient,
    StoryblokRateLimitedError,
    _iter_rows,
    _StoryblokConfig,
    render_story,
    storyblok_source,
)

TOKEN = "AbCdEfGhIjKlMnOpQrStUvwx"


def _run(blok, state, stats=None, check_active=None, **overrides):
    stats = stats if stats is not None else {}
    config = _StoryblokConfig(**overrides)
    return list(_iter_rows(blok, config, state, stats, check_active))


def _live(rows):
    return sorted(row["id"] for row in rows if not row.get("_deleted"))


def _gone(rows):
    return sorted(row["id"] for row in rows if row.get("_deleted"))


def _doc(story_id):
    return f"story:{story_id}"


# ---------------------------------------------------------------------------
# StoryblokClient over httpx.MockTransport
# ---------------------------------------------------------------------------
def _client(responses, **kwargs):
    queue = list(responses)
    requests, sleeps = [], []

    def handler(request):
        requests.append(request)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    client = StoryblokClient(
        TOKEN,
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
        **kwargs,
    )
    return client, requests, sleeps


def test_client_sends_the_token_as_a_query_parameter():
    client, requests, _ = _client([httpx.Response(200, json={"space": {}})])
    assert client.get("/v2/cdn/spaces/me", {"cv": 5}) == {"space": {}}
    url = requests[0].url
    assert url.host == "api.storyblok.com" and url.path == "/v2/cdn/spaces/me"
    assert url.params["token"] == TOKEN and url.params["cv"] == "5"
    assert TOKEN not in repr(client)


def test_the_token_stays_out_of_httpx_logs(caplog):
    client, _, _ = _client([httpx.Response(200, json={})])
    with caplog.at_level(logging.DEBUG, logger="httpx"):
        client.get("/v2/cdn/stories", {"page": 1})
    assert "/v2/cdn/stories" in caplog.text
    assert TOKEN not in caplog.text


def test_client_honours_retry_after_and_retries_5xx():
    client, requests, sleeps = _client(
        [
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(503),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    assert client.get("/v2/cdn/spaces/me") == {"ok": True}
    assert len(requests) == 3 and sum(sleeps) == 2.0 + 2.0


def test_client_stops_on_a_long_retry_after_or_an_endless_429():
    client, _, sleeps = _client([httpx.Response(429, headers={"Retry-After": "120"})])
    with pytest.raises(StoryblokRateLimitedError):
        client.get("/v2/cdn/spaces/me")
    assert sleeps == []
    client, requests, _ = _client([httpx.Response(429)])
    with pytest.raises(StoryblokRateLimitedError):
        client.get("/v2/cdn/spaces/me")
    assert len(requests) == 5


def test_client_wait_budget_stops_retries_until_reset():
    client, _, _ = _client([httpx.Response(503)], max_sleep=2.0)
    with pytest.raises(StoryblokRateLimitedError, match="wait budget"):
        client.get("/v2/cdn/spaces/me")
    client.reset_sleep_budget()
    assert client._slept == 0.0


def test_client_errors_carry_the_status_only():
    client, _, _ = _client([httpx.Response(404, json={"error": f"not found {TOKEN}"})])
    with pytest.raises(StoryblokAPIError) as info:
        client.get("/v2/cdn/stories/x")
    assert info.value.status == 404 and TOKEN not in str(info.value)
    client, _, _ = _client([httpx.Response(401, json={"error": "Unauthorized"})])
    with pytest.raises(StoryblokAuthError):
        client.get("/v2/cdn/spaces/me")


@pytest.mark.parametrize("token", ["", "   ", "tok en", "tökén", "tok\tx"])
def test_client_rejects_bad_tokens_without_echoing_them(token):
    with pytest.raises(ValueError) as info:
        StoryblokClient(token)
    assert token.strip() == "" or token.strip() not in str(info.value)


def test_client_waits_in_short_steps_asking_check_active_between():
    checks = []
    client, _, sleeps = _client(
        [httpx.Response(429, headers={"Retry-After": "3"}), httpx.Response(200, json={})],
        check_active=lambda: checks.append(1),
    )
    client.get("/v2/cdn/spaces/me")
    assert sleeps == [1.0, 1.0, 1.0] and len(checks) == 4


def test_client_close_closes_its_http_client():
    client, _, _ = _client([httpx.Response(200, json={})])
    client.get("/v2/cdn/spaces/me")
    made = client._http
    client.close()
    assert made.is_closed and client._http is None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def test_render_story_has_details_and_text_but_no_links_or_metadata(blok):
    story_id = blok.add(
        "Pricing", slug="docs/pricing", body="Plans start at 10 EUR.", tags=["b", "a"]
    )
    row = render_story(blok.published[story_id], SPACE_ID)
    assert row["id"] == _doc(story_id)
    assert row["title"] == "Storyblok story: Pricing"
    assert row["url"] == f"https://app.storyblok.com/#/me/spaces/{SPACE_ID}/stories/0/0/{story_id}"
    content = row["content"]
    assert "Path: docs/pricing" in content and "Content type: page" in content
    assert "Tags: a, b" in content and "First published: 2026-10-20 10:05 UTC" in content
    assert "Plans start at 10 EUR." in content and "Pricing hero image" in content
    for leaked in ("_editable", "storyblok#", "11111111-2222", "hero.png", "example.com"):
        assert leaked not in content


def test_rich_text_keeps_headings_lists_and_embedded_blocks():
    story = {
        "id": 1,
        "name": "Guide",
        "full_slug": "guide",
        "content": {
            "component": "article",
            "body": {
                "type": "doc",
                "content": [
                    {
                        "type": "heading",
                        "attrs": {"level": 2},
                        "content": [{"type": "text", "text": "Install"}],
                    },
                    {
                        "type": "bullet_list",
                        "content": [
                            {
                                "type": "list_item",
                                "content": [
                                    {
                                        "type": "paragraph",
                                        "content": [
                                            {"type": "text", "text": "Run "},
                                            {"type": "text", "text": "setup", "marks": [{}]},
                                        ],
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "type": "blok",
                        "attrs": {
                            "body": [{"component": "note", "_uid": "x", "text": "Be careful"}]
                        },
                    },
                ],
            },
        },
    }
    content = render_story(story, SPACE_ID)["content"]
    assert content.endswith("Install\nRun setup\nBe careful")


def test_nested_blocks_are_read_in_field_order():
    story = {
        "id": 2,
        "name": "Home",
        "content": {
            "component": "page",
            "body": [
                {"component": "hero", "_uid": "a", "headline": "Welcome", "subline": "Fast docs"},
                {
                    "component": "grid",
                    "_uid": "b",
                    "columns": [{"component": "card", "text": "One"}],
                },
            ],
        },
    }
    content = render_story(story, SPACE_ID)["content"]
    assert content.endswith("Welcome\nFast docs\nOne")


def test_settings_are_left_out_and_numbers_keep_their_field_name():
    story = {
        "id": 3,
        "name": "Pricing",
        "content": {
            "component": "default-page",
            "body": [
                {
                    "component": "price-card",
                    "headline": "Starter",
                    "price": "999",
                    "seats": 5,
                    "most_popular": True,
                    "background_color": "primary-dark",
                    "size": "medium",
                    "accent": "#80efac",
                    "text": "Perfect for small teams.",
                }
            ],
        },
    }
    content = render_story(story, SPACE_ID)["content"]
    assert content.endswith("Starter\nprice: 999\nseats: 5\nPerfect for small teams.")
    for setting in ("primary-dark", "medium", "#80efac", "True"):
        assert setting not in content


def test_secrets_in_story_text_are_scrubbed(blok):
    blok.add("Ops", body="Use AKIAABCDEFGHIJKLMNOP for the bucket")
    stats = {}
    rows = _run(blok, {}, stats=stats)
    assert "AKIAABCDEFGHIJKLMNOP" not in rows[0]["content"]
    assert "[REDACTED:aws_access_key]" in rows[0]["content"] and stats["redacted"] == 1


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
def test_first_sync_emits_every_story_and_an_unchanged_space_costs_one_request(blok):
    a, b = blok.add("Pricing"), blok.add("About")
    state, stats = {}, {}
    assert _live(_run(blok, state, stats=stats)) == sorted([_doc(a), _doc(b)])
    assert stats["emitted"] == 2 and stats["scanned"] == 2
    blok.calls.clear()
    stats = {}
    assert _run(blok, state, stats=stats) == []
    assert blok.paths() == ["/v2/cdn/spaces/me"] and stats["unchanged"] == 2


def test_the_listing_asks_for_the_current_cache_version(blok):
    blok.add("Pricing")
    _run(blok, {})
    params = next(p for path, p in blok.calls if path == "/v2/cdn/stories")
    assert params["cv"] == blok.cv and params["version"] == "published"
    assert params["per_page"] == 100


def test_a_published_edit_emits_only_that_story(blok):
    a, _b = blok.add("Pricing"), blok.add("About")
    state = {}
    _run(blok, state)
    blok.edit(a, "New plans from 12 EUR.")
    rows = _run(blok, state)
    assert _live(rows) == [_doc(a)] and "New plans from 12 EUR." in rows[0]["content"]


def test_unpublished_and_deleted_stories_are_forgotten(blok):
    a, b, c = blok.add("Pricing"), blok.add("About"), blok.add("Jobs")
    state, stats = {}, {}
    _run(blok, state)
    blok.unpublish(a)
    blok.delete(b)
    rows = _run(blok, state, stats=stats)
    assert _gone(rows) == sorted([_doc(a), _doc(b)]) and stats["deleted"] == 2
    assert list(state["stories"]) == [str(c)]


def test_drafts_are_synced_with_version_draft_on_every_run(blok):
    a = blok.add("Pricing")
    draft = blok.add("Roadmap", publish=False)
    state = {}
    assert _live(_run(blok, state, version="draft")) == sorted([_doc(a), _doc(draft)])
    blok.edit(a, "Draft only change", publish=False)  # the cache version does not move
    rows = _run(blok, state, version="draft")
    assert _live(rows) == [_doc(a)] and "Draft only change" in rows[0]["content"]


def test_an_empty_listing_waits_for_confirmation(blok):
    a = blok.add("Pricing")
    state = {}
    _run(blok, state)
    blok.unpublish(a)
    assert _run(blok, state) == [] and state["empty_listing"] is True
    assert _gone(_run(blok, state)) == [_doc(a)] and "empty_listing" not in state


def test_a_listing_without_its_list_raises_and_forgets_nothing(blok):
    blok.add("Pricing")
    state = {}
    _run(blok, state)
    blok.add("About")  # move the cache version so the listing is read
    blok.missing_list = True
    with pytest.raises(StoryblokAPIError, match="without a story list"):
        _run(blok, state)
    assert len(state["stories"]) == 1


def test_a_listed_story_without_its_id_raises(blok):
    story_id = blok.add("Pricing")
    blok.published[story_id]["id"] = None
    with pytest.raises(StoryblokAPIError, match="without its id"):
        _run(blok, {})


def test_the_listing_pages_through_every_page(blok, monkeypatch):
    monkeypatch.setattr(module, "_PAGE_SIZE", 2)
    ids = [blok.add(f"Story {n}") for n in range(5)]
    assert _live(_run(blok, {})) == sorted(_doc(i) for i in ids)
    pages = [p["page"] for path, p in blok.calls if path == "/v2/cdn/stories"]
    assert pages == [1, 2, 3]


def test_selection_reaches_the_listing(blok):
    docs = blok.add("Install", slug="docs/install", tags=["guide"], component="article")
    blok.add("Home", slug="home")
    rows = _run(blok, {}, starts_with="docs/", content_type="article", with_tag="guide")
    assert _live(rows) == [_doc(docs)]
    params = next(p for path, p in blok.calls if path == "/v2/cdn/stories")
    assert params["starts_with"] == "docs/" and params["content_type"] == "article"
    assert params["with_tag"] == "guide"


def test_another_space_under_the_same_state_raises(blok):
    blok.add("Pricing")
    state = {}
    _run(blok, state)
    with pytest.raises(ValueError, match="another Storyblok space"):
        _run(FakeStoryblok(space_id=999), state)
    assert len(state["stories"]) == 1


def test_full_resync_emits_every_story_again(blok):
    a = blok.add("Pricing")
    state = {}
    _run(blok, state)
    assert _live(_run(blok, state, full_resync=True)) == [_doc(a)]


def test_budget_stops_after_the_first_page_and_lists_again_next_run(blok, monkeypatch):
    monkeypatch.setattr(module, "_PAGE_SIZE", 1)
    a = blok.add("Pricing")
    blok.add("About")
    blok.add("Jobs")
    state = {}
    _run(blok, state)
    blok.unpublish(a)
    stats = {}
    rows = _run(blok, state, stats=stats, max_requests=2)
    assert stats["failed"] == 1 and stats["failed_budget"] == 1
    assert _gone(rows) == [], "an incomplete listing forgets nothing"
    assert set(module._STATS) <= set(stats)
    assert _gone(_run(blok, state)) == [_doc(a)], "the cache version did not move, so it lists"


def test_a_rate_limit_stops_cleanly(blok):
    blok.add("Pricing")
    blok.rate_limited.add("/v2/cdn/stories")
    stats = {}
    assert _run(blok, {}, stats=stats) == []
    assert stats["failed"] == 1 and stats["failed_rate_limit"] == 1


def test_a_token_revoked_mid_run_stops_cleanly_but_a_bad_token_raises(blok, monkeypatch):
    monkeypatch.setattr(module, "_PAGE_SIZE", 1)
    blok.add("Pricing")
    blok.add("About")
    original = blok.get

    def revoke_after_first_page(path, params=None):
        if path == "/v2/cdn/stories" and int((params or {}).get("page", 1)) > 1:
            raise StoryblokAuthError("Storyblok rejected the access token (HTTP 401)")
        return original(path, params)

    blok.get = revoke_after_first_page
    stats = {}
    rows = _run(blok, {}, stats=stats)
    assert len(rows) == 1 and stats["failed_auth"] == 1
    blok.get = original
    blok.bad_token = True
    with pytest.raises(StoryblokAuthError):
        _run(blok, {})


def test_check_active_runs_before_every_request(blok, monkeypatch):
    monkeypatch.setattr(module, "_PAGE_SIZE", 1)
    blok.add("Pricing")
    blok.add("About")
    checks = []
    _run(blok, {}, check_active=lambda: checks.append(len(blok.calls)))
    assert checks == [0, 1, 2, 3]


def test_other_errors_raise_and_keep_the_previous_state(blok):
    blok.add("Pricing")
    state = {}
    _run(blok, state)
    blok.add("About")
    before = json.dumps(state, sort_keys=True)
    blok.errors["/v2/cdn/stories"] = 500
    with pytest.raises(StoryblokAPIError):
        _run(blok, state)
    assert json.dumps(state, sort_keys=True) == before


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def test_source_declares_document_path_and_scope(blok):
    from cognee.tasks.ingestion import dlt_utils

    source = storyblok_source(service=blok)
    assert getattr(source, dlt_utils.DOCUMENT_SOURCE_ATTR) == "storyblok"
    assert getattr(source, dlt_utils.PIPELINE_SCOPE_ATTR) == "storyblok"


def test_default_resource_name_follows_the_selection(blok):
    names = {
        storyblok_source(service=blok).name,
        storyblok_source(service=blok, version="draft").name,
        storyblok_source(service=blok, starts_with="docs/").name,
        storyblok_source(service=blok, language="de").name,
    }
    assert len(names) == 4 and "storyblok" in names


@pytest.mark.parametrize("name", ["", "Has-Caps", "a" * 65, "with space", 3])
def test_invalid_resource_name_is_rejected(blok, name):
    with pytest.raises(ValueError, match="resource_name"):
        storyblok_source(service=blok, resource_name=name)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"region": "mars"},
        {"version": "latest"},
        {"max_requests": 0},
        {"starts_with": " "},
        {"with_tag": ""},
    ],
)
def test_settings_are_validated(blok, kwargs):
    with pytest.raises(ValueError):
        storyblok_source(service=blok, **kwargs)


def test_a_token_is_required_and_never_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("STORYBLOK_TOKEN", TOKEN)
    with pytest.raises(ValueError, match="token is required"):
        storyblok_source()


@pytest.mark.parametrize("region", ["eu", "us", "ca", "ap", "cn", "US"])
def test_the_region_picks_the_api_host(monkeypatch, region):
    seen = {}

    def build(token, api_url, **kwargs):
        seen.update(token=token, api_url=api_url, **kwargs)
        return "c"

    monkeypatch.setattr(module, "StoryblokClient", build)
    storyblok_source(token=TOKEN, region=region)
    assert seen == {"token": TOKEN, "api_url": REGION_HOSTS[region.lower()], "check_active": None}


# ---------------------------------------------------------------------------
# dlt pipeline: incremental sync + forget-on-delete
# ---------------------------------------------------------------------------
@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def _pipeline(dlt, tmp_path):
    db_path = (tmp_path / "storyblok.db").as_posix()
    return dlt.pipeline(
        pipeline_name="storyblok_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="storyblok_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def _staged(pipeline, table="storyblok"):
    with pipeline.sql_client() as client, client.execute_query(f"SELECT id FROM {table}") as cursor:
        return {row[0] for row in cursor.fetchall()}


def test_pipeline_syncs_incrementally_and_forgets_an_unpublished_story(dlt_mod, tmp_path, blok):
    a, b = blok.add("Pricing"), blok.add("About")
    pipeline = _pipeline(dlt_mod, tmp_path)
    pipeline.run(storyblok_source(service=blok))
    assert _staged(pipeline) == {_doc(a), _doc(b)}

    # The state lives in dlt, so a fresh source object resumes from it.
    source = storyblok_source(service=blok)
    pipeline.run(source)
    assert source.cognee_sync_stats["emitted"] == 0

    blok.unpublish(b)
    pipeline.run(storyblok_source(service=blok))
    assert _staged(pipeline) == {_doc(a)}


def test_owned_client_is_closed_and_stats_carry_every_key(dlt_mod, tmp_path, blok, monkeypatch):
    closed = []

    class Owned:
        def __init__(self, token, api_url, **kwargs):
            pass

        def get(self, path, params=None):
            return blok.get(path, params)

        def close(self):
            closed.append(True)

    monkeypatch.setattr(module, "StoryblokClient", Owned)
    blok.add("Pricing")
    source = storyblok_source(token=TOKEN)
    _pipeline(dlt_mod, tmp_path).run(source)
    assert closed == [True]
    assert set(module._STATS) <= set(source.cognee_sync_stats)
    assert source.cognee_sync_stats["failed"] == 0
