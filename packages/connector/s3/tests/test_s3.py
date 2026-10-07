import boto3
import dlt
import pytest
from cognee_community_connector_s3.s3 import S3InventoryError, _inventory, s3_source
from dlt.pipeline.exceptions import PipelineStepFailed

BUCKET = "freq-s3-test"
PREFIX = "docs/"


@pytest.fixture
def s3():
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket=BUCKET)
    return client


def put(client, key, body):
    client.put_object(Bucket=BUCKET, Key=key, Body=body.encode())


def pipeline(tmp_path):
    return dlt.pipeline(
        pipeline_name="s3_frequency",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{(tmp_path / 's3.db').as_posix()}"),
        dataset_name="s3",
        pipelines_dir=str(tmp_path / "state"),
    )


def rows(pipe):
    with (
        pipe.sql_client() as client,
        client.execute_query("select id, content from s3_objects") as cursor,
    ):
        return {row[0]: row[1] for row in cursor.fetchall()}


def run(tmp_path, client, **kwargs):
    pipe = pipeline(tmp_path)
    pipe.run(
        s3_source(BUCKET, PREFIX, client=client, **kwargs),
        write_disposition="merge",
        primary_key="id",
    )
    return pipe


def test_prefix_and_pagination(s3):
    for i in range(5):
        put(s3, f"docs/{i}.txt", str(i))
    put(s3, "other/x.txt", "x")
    assert len(_inventory(s3, BUCKET, PREFIX, 10)) == 5


def test_limit_fails_closed_before_reconciliation(s3):
    for i in range(3):
        put(s3, f"docs/{i}.txt", str(i))
    with pytest.raises(S3InventoryError, match="partial reconciliation"):
        _inventory(s3, BUCKET, PREFIX, 2)


def test_noop_and_changed_object(s3, tmp_path):
    put(s3, "docs/a.txt", "one")
    pipe = run(tmp_path, s3)
    assert rows(pipe) == {"s3://freq-s3-test/docs/a.txt": "one"}

    pipe = run(tmp_path, s3)
    assert rows(pipe) == {"s3://freq-s3-test/docs/a.txt": "one"}

    put(s3, "docs/a.txt", "two")
    pipe = run(tmp_path, s3)
    assert rows(pipe) == {"s3://freq-s3-test/docs/a.txt": "two"}


def test_present_but_oversize_is_not_treated_as_deleted(s3, tmp_path):
    put(s3, "docs/a.txt", "small")
    run(tmp_path, s3, max_object_size=100)

    put(s3, "docs/a.txt", "x" * 200)
    pipe = run(tmp_path, s3, max_object_size=100)
    assert rows(pipe) == {"s3://freq-s3-test/docs/a.txt": "small"}


def test_deleted_last_object_physically_leaves_staging(s3, tmp_path):
    put(s3, "docs/a.txt", "a")
    run(tmp_path, s3)
    s3.delete_object(Bucket=BUCKET, Key="docs/a.txt")

    pipe = run(tmp_path, s3)
    assert rows(pipe) == {}


def test_read_race_aborts_instead_of_deleting(s3, tmp_path):
    put(s3, "docs/a.txt", "a")

    class RaceClient:
        def get_paginator(self, name):
            return s3.get_paginator(name)

        def get_object(self, **kwargs):
            put(s3, "docs/a.txt", "changed-after-list")
            return s3.get_object(**kwargs)

    with pytest.raises(PipelineStepFailed, match="object changed or became unreadable"):
        run(tmp_path, RaceClient())
