"""Tests for DLT resource contract and SQLite state persistence."""

import dlt
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from fake_jsr import FakeJSRClient

from cognee_community_connector_jsr.jsr import jsr_source


def test_resource_attributes_and_contract():
    fake = FakeJSRClient()
    src = jsr_source(scopes=["std"], client=fake)

    assert "jsr_packages" in src.resources
    res = src.resources["jsr_packages"]
    assert res.write_disposition == "merge"
    assert getattr(res, DOCUMENT_SOURCE_ATTR) == "jsr"


def test_dlt_pipeline_persistence_across_runs(tmp_path):
    fake = FakeJSRClient()
    pipeline = dlt.pipeline(
        pipeline_name="test_jsr_persistence",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'jsr_test.db'}"),
        dataset_name="jsr_data",
    )

    # First ingestion run
    src1 = jsr_source(scopes=["std"], client=fake)
    info1 = pipeline.run(src1)
    assert info1.has_failed_jobs is False

    # Second ingestion run - should be incremental
    src2 = jsr_source(scopes=["std"], client=fake)
    info2 = pipeline.run(src2)
    assert info2.has_failed_jobs is False
