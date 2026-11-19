"""webflow_source through a real dlt pipeline (temp sqlite destination).

Checks what cognee reads back from staging: one table per collection plus the
collection cards, tombstones that really remove rows on merge, the cursor state
that survives between runs, and the guard against any write disposition other
than merge.
"""

import dlt
import httpx
import pytest
from cognee.tasks.ingestion import dlt_utils
from fake_webflow import TOKEN, FakeWebflow

from cognee_community_connector_webflow import WebflowClient, webflow_source
from cognee_community_connector_webflow import webflow as webflow_module


@pytest.fixture
def fake():
    fake = FakeWebflow()
    fake.site = fake.add_site("Acme", "acme")
    fake.posts = fake.add_collection(
        fake.site,
        "posts",
        fields=[
            {"slug": "name", "displayName": "Name", "type": "PlainText"},
            {"slug": "body", "displayName": "Body", "type": "PlainText"},
        ],
    )
    fake.alpha = fake.add_item(fake.posts, "Alpha", body="First")
    fake.bravo = fake.add_item(fake.posts, "Bravo", body="Second")
    return fake


def _client(fake) -> WebflowClient:
    return WebflowClient(TOKEN, transport=fake.transport(), sleep=lambda _s: None)


def _pipeline(tmp_path):
    return dlt.pipeline(
        pipeline_name="webflow_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{(tmp_path / 'stage.db').as_posix()}"),
        dataset_name="webflow_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def _run(tmp_path, fake, **kwargs):
    source = webflow_source(client=_client(fake), **kwargs)
    _pipeline(tmp_path).run(
        source, write_disposition="merge", primary_key="id", columns=dlt_utils.NODE_SET_COLUMN_HINT
    )
    return source


def _ids(tmp_path, table):
    with (
        _pipeline(tmp_path).sql_client() as client,
        client.execute_query(f"SELECT id FROM {table}") as cursor,
    ):
        return {row[0] for row in cursor.fetchall()}


def test_items_and_cards_land_in_their_own_staging_tables(tmp_path, fake):
    source = _run(tmp_path, fake)

    assert _ids(tmp_path, f"webflow_items_{fake.posts}") == {
        f"webflow:item:{fake.alpha}",
        f"webflow:item:{fake.bravo}",
    }
    assert _ids(tmp_path, "webflow_collection_cards") == {f"webflow:collection:{fake.posts}"}
    assert source.cognee_sync_stats["items"] == 2


def test_upstream_delete_removes_the_staged_row(tmp_path, fake):
    _run(tmp_path, fake)
    fake.delete_item(fake.posts, fake.alpha)

    source = _run(tmp_path, fake)

    assert _ids(tmp_path, f"webflow_items_{fake.posts}") == {f"webflow:item:{fake.bravo}"}
    assert source.cognee_sync_stats["deleted"] == 1


def test_the_cursor_survives_between_runs(tmp_path, fake):
    _run(tmp_path, fake)
    fake.edit_item(fake.posts, fake.alpha, body="Edited")

    source = _run(tmp_path, fake)

    assert source.cognee_sync_stats["items"] == 1
    assert source.cognee_sync_stats["unchanged"] == 1


def test_a_removed_collection_empties_its_staging_table(tmp_path, fake):
    _run(tmp_path, fake)
    fake.delete_collection(fake.posts)
    fake.add_collection(fake.site, "docs")

    _run(tmp_path, fake)

    assert _ids(tmp_path, f"webflow_items_{fake.posts}") == set()
    assert _ids(tmp_path, "webflow_collection_cards") != set()
    assert f"webflow:collection:{fake.posts}" not in _ids(tmp_path, "webflow_collection_cards")


def test_replace_disposition_is_refused(tmp_path, fake):
    source = webflow_source(client=_client(fake))
    with pytest.raises(Exception, match='write_disposition="merge"'):
        _pipeline(tmp_path).run(source, write_disposition="replace")


def test_source_declares_cognee_markers(fake):
    source = webflow_source(client=_client(fake), resource_name="marketing")

    assert dlt_utils.document_source_tag(source) == "webflow"
    assert getattr(source, dlt_utils.PIPELINE_SCOPE_ATTR) == "marketing"
    assert list(source.resources) == ["marketing"]


def test_resource_name_must_be_a_plain_name(fake):
    with pytest.raises(ValueError, match="resource_name"):
        webflow_source(client=_client(fake), resource_name="Marketing Site")


@pytest.mark.parametrize("argument", ["site_ids", "collections"])
def test_an_empty_selection_is_refused(fake, argument):
    with pytest.raises(ValueError, match=argument):
        webflow_source(client=_client(fake), **{argument: []})


def test_a_single_string_selects_one_collection(tmp_path, fake):
    fake.add_collection(fake.site, "docs")

    source = _run(tmp_path, fake, collections="posts")

    assert source.cognee_sync_stats["collections"] == 1


def test_the_token_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("WEBFLOW_API_TOKEN", "from-env")
    seen = []

    def handler(request):
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json={"sites": []})

    client = WebflowClient(transport=httpx.MockTransport(handler))
    client.get("/sites")

    assert seen == ["Bearer from-env"]
    assert webflow_source(client=client)


def test_building_the_source_without_a_token_fails_early(monkeypatch):
    monkeypatch.delenv("WEBFLOW_API_TOKEN", raising=False)
    with pytest.raises(ValueError, match="WEBFLOW_API_TOKEN"):
        webflow_source()


def test_a_source_can_be_run_again_and_builds_a_fresh_client_each_run(tmp_path, fake, monkeypatch):
    built = []

    def make_client(token):
        built.append(token)
        return _client(fake)

    monkeypatch.setattr(webflow_module, "WebflowClient", make_client)
    source = webflow_source(TOKEN)
    pipeline = _pipeline(tmp_path)

    pipeline.run(source, write_disposition="merge", primary_key="id")
    fake.edit_item(fake.posts, fake.alpha, body="Edited")
    pipeline.run(source, write_disposition="merge", primary_key="id")

    assert built == [TOKEN, TOKEN]
    assert source.cognee_sync_stats["items"] == 1


def test_a_client_passed_in_is_left_open_for_its_owner(tmp_path, fake):
    client = _client(fake)

    _pipeline(tmp_path).run(
        webflow_source(client=client), write_disposition="merge", primary_key="id"
    )

    assert not client._http.is_closed


def test_check_active_stops_a_sync_before_it_reads_or_commits_anything(tmp_path, fake):
    def revoked():
        raise PermissionError("access revoked")

    source = webflow_source(client=_client(fake), check_active=revoked)

    with pytest.raises(Exception, match="access revoked"):
        _pipeline(tmp_path).run(source, write_disposition="merge", primary_key="id")

    assert fake.calls == [], "no request may be made once access is revoked"
