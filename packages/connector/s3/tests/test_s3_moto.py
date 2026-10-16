"""Real boto3 client with Moto S3 API; no AWS access or credentials."""
import boto3
import pytest
from moto import mock_aws

from cognee_community_connector_s3.s3 import (
    load_manifest,
    snapshot_rows,
    sync_with_publisher,
)


@pytest.fixture
def s3():
    with mock_aws():
        client = boto3.client(
            "s3", region_name="us-east-1",
            aws_access_key_id="testing", aws_secret_access_key="testing",
        )
        client.create_bucket(Bucket="test-bucket")
        yield client


def test_moto_pagination_prefix_and_incremental_reconcile(s3, tmp_path):
    for i in range(1005):
        s3.put_object(Bucket="test-bucket", Key=f"docs/{i:04}.txt",
                      Body=f"document-{i}".encode())
    s3.put_object(Bucket="test-bucket", Key="other/private.txt", Body=b"outside")
    path = tmp_path / "checkpoint.json"
    seen = []
    assert sync_with_publisher(s3, "test-bucket", "docs/", path,
                               lambda rows: seen.append(rows), max_objects=2000) == 1005
    assert len(seen[0]) == 1005
    assert all(row["id"].startswith("s3://test-bucket/docs/") for row in seen[0])
    s3.put_object(Bucket="test-bucket", Key="docs/0001.txt", Body=b"changed")
    s3.delete_object(Bucket="test-bucket", Key="docs/0002.txt")
    sync_with_publisher(s3, "test-bucket", "docs/", path,
                        lambda rows: seen.append(rows), max_objects=2000)
    assert len(seen[1]) == 1004
    assert next(row for row in seen[1] if row["title"] == "0001.txt")["content"] == "changed"
    assert not any(row["title"] == "0002.txt" for row in seen[1])
    assert len(load_manifest(path, "test-bucket", "docs/")) == 1004


def test_moto_last_object_deletion(s3, tmp_path):
    s3.put_object(Bucket="test-bucket", Key="docs/only.txt", Body=b"one")
    path = tmp_path / "checkpoint.json"
    published = []
    sync_with_publisher(s3, "test-bucket", "docs/", path, published.append)
    s3.delete_object(Bucket="test-bucket", Key="docs/only.txt")
    sync_with_publisher(s3, "test-bucket", "docs/", path, published.append)
    assert len(published[0]) == 1
    assert published[1] == []
    assert load_manifest(path, "test-bucket", "docs/") == {}


def test_moto_read_failure_does_not_advance_checkpoint(s3, tmp_path):
    s3.put_object(Bucket="test-bucket", Key="docs/only.txt", Body=b"one")
    path = tmp_path / "checkpoint.json"
    sync_with_publisher(s3, "test-bucket", "docs/", path, lambda rows: None)
    old = load_manifest(path, "test-bucket", "docs/")
    s3.put_object(Bucket="test-bucket", Key="docs/only.txt", Body=b"new")
    class BrokenClient:
        def get_paginator(self, name):
            return s3.get_paginator(name)
        def get_object(self, **kwargs):
            raise PermissionError("injected failure")
    with pytest.raises(PermissionError):
        sync_with_publisher(BrokenClient(), "test-bucket", "docs/", path,
                            lambda rows: None)
    assert load_manifest(path, "test-bucket", "docs/") == old
