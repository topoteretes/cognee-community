"""The ``fathom_source`` factory: arguments, the document marker and the resource contract."""

from datetime import datetime, timezone

import pytest
from cognee.tasks.ingestion.dlt_utils import (
    DOCUMENT_SOURCE_ATTR,
    PIPELINE_SCOPE_ATTR,
    document_source_tag,
)

from cognee_community_connector_fathom import FathomClient, fathom_source

KEY = "SECRET-fathom-key-123"


def _source(**kwargs):
    kwargs.setdefault("api_key", KEY)
    return fathom_source(**kwargs)


def test_the_source_opts_into_the_document_path_and_its_own_scope():
    source = _source(resource_name="fathom_sales")

    assert document_source_tag(source) == "fathom"
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == "fathom"
    assert getattr(source, PIPELINE_SCOPE_ATTR) == "fathom_sales"
    assert source.name == "fathom_sales"
    assert source.cognee_sync_stats == {}


def test_the_resource_is_a_merge_on_id_with_a_hard_delete_column():
    source = _source()

    assert source.name == "fathom_meetings"
    assert source.write_disposition == "merge"
    assert source.compute_table_schema()["columns"]["_deleted"]["hard_delete"] is True
    assert source.compute_table_schema()["columns"]["id"]["primary_key"] is True


def test_the_key_falls_back_to_the_environment(monkeypatch):
    monkeypatch.setenv("FATHOM_API_KEY", KEY)
    assert fathom_source() is not None

    monkeypatch.delenv("FATHOM_API_KEY")
    with pytest.raises(ValueError, match="FATHOM_API_KEY"):
        fathom_source()


def test_a_prebuilt_client_needs_no_key(monkeypatch):
    monkeypatch.delenv("FATHOM_API_KEY", raising=False)
    assert fathom_source(client=FathomClient(KEY)) is not None


def test_the_key_is_not_reachable_from_the_resource():
    source = _source()
    assert KEY not in repr(source)
    assert KEY not in repr(vars(source))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"recheck_days": -1},
        {"recheck_days": 1.5},
        {"recheck_days": True},
        {"max_delete_fraction": 1.5},
        {"max_delete_fraction": -0.1},
        {"resource_name": "has space"},
        {"resource_name": ""},
        {"resource_name": "1abc"},
        {"since": "yesterday"},
        {"teams": [""]},
        {"recorded_by": [3]},
        {
            "include_summary": False,
            "include_action_items": False,
            "include_transcript": False,
            "include_highlights": False,
        },
    ],
)
def test_bad_arguments_fail_at_construction(kwargs):
    with pytest.raises(ValueError):
        _source(**kwargs)


def test_a_single_team_string_and_naive_since_are_accepted():
    assert _source(teams="Sales", recorded_by="ann@ourco.com") is not None
    assert _source(since=datetime(2026, 1, 1)) is not None
    assert _source(since=datetime(2026, 1, 1, tzinfo=timezone.utc)) is not None
    assert _source(since="2026-01-01T00:00:00Z") is not None
