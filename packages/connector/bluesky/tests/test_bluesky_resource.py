"""Tests for DLT resource contract and SQLite state persistence."""

import dlt
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from fake_bluesky import FakeBlueskyClient

from cognee_community_connector_bluesky.bluesky import bluesky_source


def test_resource_attributes_and_contract():
    fake = FakeBlueskyClient()
    src = bluesky_source(actors=["bsky.app"], client=fake)

    assert "bluesky_posts" in src.resources
    res = src.resources["bluesky_posts"]
    assert res.write_disposition == "merge"
    assert getattr(res, DOCUMENT_SOURCE_ATTR) == "bluesky"


def test_dlt_pipeline_persistence_across_runs(tmp_path):
    fake = FakeBlueskyClient()
    pipeline = dlt.pipeline(
        pipeline_name="test_bluesky_persistence",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'bsky_test.db'}"),
        dataset_name="bsky_data",
    )

    src1 = bluesky_source(actors=["bsky.app"], client=fake)
    info1 = pipeline.run(src1)
    assert info1.has_failed_jobs is False

    src2 = bluesky_source(actors=["bsky.app"], client=fake)
    info2 = pipeline.run(src2)
    assert info2.has_failed_jobs is False
