"""S3 -> real dlt SQLite replacement, with no external AWS or LLM access."""
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
