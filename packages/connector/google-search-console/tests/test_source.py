"""google_search_console_source through a real dlt pipeline (temp sqlite destination).

Checks what cognee reads back from staging: a table per property, tombstones that really remove
rows on merge, the state that survives between runs, the guard against any write disposition
other than merge, and the argument checks.
"""

from datetime import date

import dlt
import pytest
from cognee.tasks.ingestion import dlt_utils
from fake_search_console import ACCESS_TOKEN, FakeSearchConsole
from test_sync import DOCS, OTHER, SITE

from cognee_community_connector_google_search_console import (
    SearchConsoleClient,
    SearchConsoleCredentials,
    google_search_console_source,
    list_properties,
)


@pytest.fixture
def fake(today):
    fake = FakeSearchConsole(today)
    fake.add_site(SITE)
    fake.add_traffic(SITE, date(2026, 10, 5), "cognee", DOCS, clicks=4, impressions=40)
    fake.add_traffic(SITE, date(2026, 9, 5), "cognee", DOCS, clicks=3, impressions=30)
    return fake


def _client(fake) -> SearchConsoleClient:
    return SearchConsoleClient(
        SearchConsoleCredentials(access_token=ACCESS_TOKEN),
        transport=fake.transport(),
        sleep=lambda _s: None,
    )


def _pipeline(tmp_path):
    return dlt.pipeline(
        pipeline_name="gsc_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{(tmp_path / 'stage.db').as_posix()}"),
        dataset_name="gsc_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def _run(tmp_path, fake, today, **kwargs):
    source = google_search_console_source(client=_client(fake), today=today, **kwargs)
    _pipeline(tmp_path).run(
        source, write_disposition="merge", primary_key="id", columns=dlt_utils.NODE_SET_COLUMN_HINT
    )
    return source


def _ids(tmp_path) -> dict[str, set[str]]:
    """Staged row ids by table."""
    pipeline = _pipeline(tmp_path)
    with pipeline.sql_client() as client:
        tables = [t for t in pipeline.default_schema.data_table_names() if t.startswith("google")]
        result = {}
        for table in tables:
            with client.execute_query(f"SELECT id FROM {table}") as cursor:
                result[table] = {row[0] for row in cursor.fetchall()}
        return result


def _all_ids(tmp_path) -> set[str]:
    return set().union(*_ids(tmp_path).values())


def test_documents_land_in_a_staging_table_per_property(tmp_path, fake, today):
    fake.add_site(OTHER)
    fake.add_traffic(OTHER, date(2026, 10, 5), "blog", "https://blog.example.com/a", clicks=2)

    source = _run(tmp_path, fake, today, months=2)

    tables = _ids(tmp_path)
    assert len(tables) == 2
    assert source.cognee_sync_stats["sites"] == 2
    assert all(row_id.startswith("google_search_console:") for row_id in _all_ids(tmp_path))
    mine = next(
        ids for ids in tables.values() if f"google_search_console:overview:{SITE}:2026-10" in ids
    )
    assert all(f":{SITE}:" in row_id for row_id in mine), "a table holds one property"


def test_deselecting_a_property_removes_its_staged_rows(tmp_path, fake, today):
    fake.add_site(OTHER)
    fake.add_traffic(OTHER, date(2026, 10, 5), "blog", "https://blog.example.com/a", clicks=2)
    _run(tmp_path, fake, today, months=2)
    before = _all_ids(tmp_path)

    source = _run(tmp_path, fake, today, months=2, sites=[OTHER])

    after = _all_ids(tmp_path)
    assert after and after < before
    assert all(f":{OTHER}:" in row_id for row_id in after)
    assert source.cognee_sync_stats["deleted"] == len(before - after)


def test_the_state_survives_between_runs(tmp_path, fake, today):
    _run(tmp_path, fake, today, months=2)

    source = _run(tmp_path, fake, today, months=2)

    assert source.cognee_sync_stats["documents"] == 0
    assert source.cognee_sync_stats["closed"] == 1
    assert source.cognee_sync_stats["unchanged"] == 2


def test_new_traffic_updates_the_staged_row_in_place(tmp_path, fake, today):
    _run(tmp_path, fake, today, months=2)
    before = _all_ids(tmp_path)
    fake.add_traffic(SITE, date(2026, 10, 14), "cognee", DOCS, clicks=5, impressions=50)

    source = _run(tmp_path, fake, today, months=2)

    assert _all_ids(tmp_path) == before, "merged by id, not appended"
    assert source.cognee_sync_stats["documents"] == 2


def test_replace_disposition_is_refused(tmp_path, fake, today):
    source = google_search_console_source(client=_client(fake), today=today)
    with pytest.raises(Exception, match='write_disposition="merge"'):
        _pipeline(tmp_path).run(source, write_disposition="replace")


def test_source_declares_cognee_markers(fake):
    source = google_search_console_source(client=_client(fake), resource_name="marketing")

    assert dlt_utils.document_source_tag(source) == "google_search_console"
    assert getattr(source, dlt_utils.PIPELINE_SCOPE_ATTR) == "marketing"
    assert list(source.resources) == ["marketing"]


def test_resource_name_must_be_a_plain_name(fake):
    with pytest.raises(ValueError, match="resource_name"):
        google_search_console_source(client=_client(fake), resource_name="My Site")


@pytest.mark.parametrize(
    ("argument", "value", "message"),
    [
        ("sites", [], "sites is empty"),
        ("sites", [" "], "sites is empty"),
        ("months", 0, "months"),
        ("months", 17, "months"),
        ("search_type", "gopher", "search_type"),
        ("top_pages", -1, "cannot be negative"),
        ("top_queries", 25_001, "capped at 25,000"),
    ],
)
def test_bad_arguments_are_refused_before_anything_is_read(fake, argument, value, message):
    with pytest.raises(ValueError, match=message):
        google_search_console_source(client=_client(fake), **{argument: value})
    assert fake.calls == []


def test_a_single_string_selects_one_property(tmp_path, fake, today):
    fake.add_site(OTHER)

    source = _run(tmp_path, fake, today, sites=SITE)

    assert source.cognee_sync_stats["sites"] == 1


def test_building_the_source_without_credentials_fails_early(monkeypatch, tmp_path):
    for name in ("ACCESS_TOKEN", "REFRESH_TOKEN", "CLIENT_ID", "CLIENT_SECRET", "TOKEN_PATH"):
        monkeypatch.delenv(f"GOOGLE_SEARCH_CONSOLE_{name}", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(ValueError, match="credentials are missing"):
        google_search_console_source()


def test_list_properties_shows_what_can_be_selected(fake):
    fake.add_site(OTHER, "siteUnverifiedUser")

    assert list_properties(client=_client(fake)) == [
        {"siteUrl": SITE, "permissionLevel": "siteOwner"},
        {"siteUrl": OTHER, "permissionLevel": "siteUnverifiedUser"},
    ]


def test_a_host_can_stop_a_sync_whose_access_was_revoked(tmp_path, fake, today):
    def revoked():
        raise PermissionError("access revoked")

    source = google_search_console_source(client=_client(fake), today=today, check_active=revoked)
    with pytest.raises(Exception, match="access revoked"):
        _pipeline(tmp_path).run(source, write_disposition="merge", primary_key="id")


def test_service_is_an_alias_for_client(tmp_path, fake, today):
    source = google_search_console_source(service=_client(fake), today=today)
    _pipeline(tmp_path).run(
        source, write_disposition="merge", primary_key="id", columns=dlt_utils.NODE_SET_COLUMN_HINT
    )

    assert source.cognee_sync_stats["sites"] == 1


def test_stats_are_plain_integers_for_hosts(tmp_path, fake, today):
    source = _run(tmp_path, fake, today)

    stats = source.cognee_sync_stats
    assert all(isinstance(value, int) for value in stats.values())
    assert {"documents", "unchanged", "deleted", "failed_rate_limit"} <= set(stats)
