"""Tests for Resend DLT resource contracts and state management."""

import dlt
import pytest
from fake_resend import FakeResendClient

from cognee_community_connector_resend.resend import (
    DOCUMENT_SOURCE_ATTR,
    PIPELINE_SCOPE_ATTR,
    resend_resource,
    resend_source,
)


def test_resource_attributes():
    res = resend_resource(api_key="mock_key", client=FakeResendClient())
    assert res.DOCUMENT_SOURCE_ATTR == DOCUMENT_SOURCE_ATTR
    assert res.PIPELINE_SCOPE_ATTR == PIPELINE_SCOPE_ATTR
    assert res.name == "resend"
    assert res.write_disposition == "merge"


def test_resource_invalid_category():
    with pytest.raises(ValueError, match="Invalid categories in include"):
        resend_resource(api_key="mock_key", include=("invalid_category",))


def test_source_initialization():
    src = resend_source(api_key="mock_key", client=FakeResendClient())
    assert src.name == "resend"
    assert len(src.resources) == 1
    assert "resend" in src.resources


def test_state_persistence_across_runs(sample_data, tmp_path):
    client = FakeResendClient(sample_data=sample_data)
    pipeline = dlt.pipeline(
        pipeline_name="test_resend_state",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'resend.db'}"),
        dataset_name="test_data",
        pipelines_dir=str(tmp_path),
    )

    res1 = resend_resource(api_key="mock_key", client=client, include=("domains",))
    load_info1 = pipeline.run(res1)
    assert len(load_info1.loads_ids) > 0

    # Second run without changes should yield zero new/modified records
    res2 = resend_resource(api_key="mock_key", client=client, include=("domains",))
    items2 = list(res2)
    assert len(items2) == 0
