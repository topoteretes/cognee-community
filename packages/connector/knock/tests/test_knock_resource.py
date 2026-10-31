"""Tests for DLT resource contracts and state management."""

import pytest
from fake_knock import FakeKnockClient

from cognee_community_connector_knock.knock import (
    DOCUMENT_SOURCE_ATTR,
    PIPELINE_SCOPE_ATTR,
    knock_resource,
    knock_source,
)


def test_resource_attributes():
    res = knock_resource(api_key="mock_key", client=FakeKnockClient())
    assert res.DOCUMENT_SOURCE_ATTR == DOCUMENT_SOURCE_ATTR
    assert res.PIPELINE_SCOPE_ATTR == PIPELINE_SCOPE_ATTR
    assert res.name == "knock"
    assert res.write_disposition == "merge"


def test_resource_invalid_category():
    with pytest.raises(ValueError, match="Invalid categories in include"):
        knock_resource(api_key="mock_key", include=("invalid_category",))


def test_source_initialization():
    src = knock_source(api_key="mock_key", client=FakeKnockClient())
    assert src.name == "knock"
    assert len(src.resources) == 1
    assert "knock" in src.resources


def test_state_persistence_across_runs(sample_data, tmp_path):
    import dlt

    client = FakeKnockClient(sample_data=sample_data)
    pipeline = dlt.pipeline(
        pipeline_name="test_knock_state",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'knock.db'}"),
        dataset_name="test_data",
        pipelines_dir=str(tmp_path),
    )

    res1 = knock_resource(api_key="mock_key", client=client, include=("workflows",))
    load_info1 = pipeline.run(res1)
    assert len(load_info1.loads_ids) > 0

    # Second run without changes should yield zero new/modified records
    res2 = knock_resource(api_key="mock_key", client=client, include=("workflows",))
    items2 = list(res2)
    assert len(items2) == 0
