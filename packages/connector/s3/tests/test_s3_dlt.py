"""S3 -> real dlt DuckDB replacement, with no external AWS or LLM access."""
import boto3
import dlt
from moto import mock_aws

from cognee_community_connector_s3.s3 import s3_source


def test_real_dlt_replace_reconciles_deletion(tmp_path):
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1",
                              aws_access_key_id="testing", aws_secret_access_key="testing")
        client.create_bucket(Bucket="test-bucket")
        client.put_object(Bucket="test-bucket", Key="docs/one.txt", Body=b"one")
        client.put_object(Bucket="test-bucket", Key="docs/two.txt", Body=b"two")
        pipeline = dlt.pipeline(pipeline_name="s3_reconcile", destination=dlt.destinations.duckdb(credentials=str(tmp_path / "test.duckdb")),
                                dataset_name="s3_reconcile", pipelines_dir=str(tmp_path))
        pipeline.run(s3_source("test-bucket", "docs/", client=client))
        with pipeline.sql_client() as sql:
            assert sql.execute_sql("SELECT count(*) FROM s3_documents")[0][0] == 2
        client.delete_object(Bucket="test-bucket", Key="docs/two.txt")
        pipeline.run(s3_source("test-bucket", "docs/", client=client))
        with pipeline.sql_client() as sql:
            assert sql.execute_sql("SELECT count(*) FROM s3_documents")[0][0] == 1


def test_document_source_routes_to_cognee_document_ingestion():
    """Verify Cognee's real document-source routing contract, not only dlt rows."""
    from cognee.tasks.ingestion.dlt_utils import document_source_tag
    from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item
    from types import SimpleNamespace
    from uuid import NAMESPACE_OID, uuid5

    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1",
                              aws_access_key_id="testing", aws_secret_access_key="testing")
        source = s3_source("test-bucket", "docs/", client=client)
        assert document_source_tag(source) == "s3"
        identity = "s3://test-bucket/docs/one.txt"
        row = SimpleNamespace(row_data={"id": identity, "title": "one.txt",
                                        "url": identity, "content": "first version"},
                              content_hash="hash")
        item = _build_document_data_item(row, uuid5(NAMESPACE_OID, identity), "s3")
        assert item.external_metadata["source"] == "s3"
        assert item.external_metadata["external_id"] == identity
        assert item.data_id == uuid5(NAMESPACE_OID, identity)
        assert "first version" in item.data


def test_empty_s3_snapshot_replaces_staging_without_rows(tmp_path):
    """Regression boundary: dlt empties staging; Cognee 1.4.0 may retain graph orphans."""
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1",
                              aws_access_key_id="testing", aws_secret_access_key="testing")
        client.create_bucket(Bucket="test-bucket")
        client.put_object(Bucket="test-bucket", Key="docs/only.txt", Body=b"one")
        pipeline = dlt.pipeline(
            pipeline_name="s3_empty", dataset_name="s3_empty",
            destination=dlt.destinations.duckdb(credentials=str(tmp_path / "empty.duckdb")),
            pipelines_dir=str(tmp_path / "state"),
        )
        pipeline.run(s3_source("test-bucket", "docs/", client=client))
        client.delete_object(Bucket="test-bucket", Key="docs/only.txt")
        pipeline.run(s3_source("test-bucket", "docs/", client=client))
        with pipeline.sql_client() as sql:
            assert sql.execute_sql("SELECT count(*) FROM s3_documents")[0][0] == 0
