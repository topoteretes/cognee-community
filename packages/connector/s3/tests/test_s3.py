import boto3
import dlt
import pytest
from cognee.tasks.ingestion import dlt_utils
from dlt.pipeline.exceptions import PipelineStepFailed

from cognee_community_connector_s3.s3 import (
    S3InventoryError,
    _inventory,
    _resource_name,
    s3_source,
)

BUCKET = "cognee-s3-test"
PREFIX = "docs/"


@pytest.fixture
def s3():
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket=BUCKET)
    return client


def put(client, key, body):
    client.put_object(Bucket=BUCKET, Key=key, Body=body.encode())


def pipeline(tmp_path, name="s3_connector_test"):
    return dlt.pipeline(
        pipeline_name=name,
        destination=dlt.destinations.sqlalchemy(
            f"sqlite:///{(tmp_path / f'{name}.db').as_posix()}"
        ),
        dataset_name="s3",
        pipelines_dir=str(tmp_path / "state"),
    )


def rows(pipe, bucket=BUCKET, prefix=PREFIX):
    table = _resource_name(bucket, prefix)
    with (
        pipe.sql_client() as client,
        client.execute_query(f'SELECT id, content FROM "{table}"') as cursor,
    ):
        return {row[0]: row[1] for row in cursor.fetchall()}


def run(tmp_path, client, *, prefix=PREFIX, name="s3_connector_test", **kwargs):
    pipe = pipeline(tmp_path, name=name)
    source = s3_source(BUCKET, prefix, client=client, **kwargs)
    pipe.run(source, write_disposition="merge", primary_key="id")
    return pipe


def test_source_requires_table_scoped_cleanup(monkeypatch, s3):
    monkeypatch.setattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0)
    with pytest.raises(RuntimeError, match="table-scoped"):
        s3_source(BUCKET, PREFIX, client=s3)


def test_scope_has_unique_table_and_pipeline_identity(s3):
    a = s3_source(BUCKET, "docs/a/", client=s3)
    b = s3_source(BUCKET, "docs/b/", client=s3)
    assert a.name != b.name
    assert getattr(a, dlt_utils.PIPELINE_SCOPE_ATTR) != getattr(b, dlt_utils.PIPELINE_SCOPE_ATTR)


def test_real_pagination_and_prefix_scope(s3):
    for i in range(1005):
        put(s3, f"docs/{i:04}.txt", str(i))
    put(s3, "other/private.txt", "outside")
    inventory = _inventory(s3, BUCKET, PREFIX, 1100)
    assert len(inventory) == 1005
    assert all(key.startswith(PREFIX) for key in inventory)


def test_limit_fails_closed_before_reconciliation(s3):
    for i in range(3):
        put(s3, f"docs/{i}.txt", str(i))
    with pytest.raises(S3InventoryError, match="partial reconciliation"):
        _inventory(s3, BUCKET, PREFIX, 2)


def test_noop_and_changed_object(s3, tmp_path):
    put(s3, "docs/a.txt", "one")
    pipe = run(tmp_path, s3)
    assert rows(pipe) == {"s3://cognee-s3-test/docs/a.txt": "one"}

    pipe = run(tmp_path, s3)
    assert rows(pipe) == {"s3://cognee-s3-test/docs/a.txt": "one"}

    put(s3, "docs/a.txt", "two")
    pipe = run(tmp_path, s3)
    assert rows(pipe) == {"s3://cognee-s3-test/docs/a.txt": "two"}


def test_present_but_oversize_is_not_treated_as_deleted(s3, tmp_path):
    put(s3, "docs/a.txt", "small")
    run(tmp_path, s3, max_object_size=100)

    put(s3, "docs/a.txt", "x" * 200)
    pipe = run(tmp_path, s3, max_object_size=100)
    assert rows(pipe) == {"s3://cognee-s3-test/docs/a.txt": "small"}


def test_deleted_last_object_physically_leaves_staging(s3, tmp_path):
    put(s3, "docs/a.txt", "a")
    run(tmp_path, s3)
    s3.delete_object(Bucket=BUCKET, Key="docs/a.txt")

    pipe = run(tmp_path, s3)
    assert rows(pipe) == {}


def test_read_race_aborts_instead_of_reconciling(s3, tmp_path):
    put(s3, "docs/a.txt", "a")

    class RaceClient:
        def get_paginator(self, name):
            return s3.get_paginator(name)

        def get_object(self, **kwargs):
            put(s3, "docs/a.txt", "changed-after-list")
            return s3.get_object(**kwargs)

    with pytest.raises(PipelineStepFailed, match="object changed or became unreadable"):
        run(tmp_path, RaceClient())


def test_invalid_utf8_aborts(s3, tmp_path):
    s3.put_object(Bucket=BUCKET, Key="docs/a.txt", Body=b"\xff\xfe\xfd")
    with pytest.raises(PipelineStepFailed, match="not valid UTF-8"):
        run(tmp_path, s3)


def test_listing_failure_preserves_previous_rows(s3, tmp_path):
    put(s3, "docs/a.txt", "one")
    pipe = run(tmp_path, s3)
    assert rows(pipe) == {"s3://cognee-s3-test/docs/a.txt": "one"}

    class BrokenClient:
        def get_paginator(self, name):
            class BrokenPaginator:
                def paginate(self, **kwargs):
                    raise PermissionError("list denied")
                    yield

            return BrokenPaginator()

    with pytest.raises(PipelineStepFailed, match="listing failed"):
        run(tmp_path, BrokenClient())
    assert rows(pipe) == {"s3://cognee-s3-test/docs/a.txt": "one"}


def test_changed_object_read_failure_preserves_checkpoint_and_replays(s3, tmp_path):
    put(s3, "docs/a.txt", "one")
    pipe = run(tmp_path, s3)
    assert rows(pipe) == {"s3://cognee-s3-test/docs/a.txt": "one"}

    put(s3, "docs/a.txt", "two")

    class BrokenReadClient:
        def get_paginator(self, name):
            return s3.get_paginator(name)

        def get_object(self, **kwargs):
            raise PermissionError("get denied")

    with pytest.raises(PipelineStepFailed, match="became unreadable"):
        run(tmp_path, BrokenReadClient())
    assert rows(pipe) == {"s3://cognee-s3-test/docs/a.txt": "one"}

    pipe = run(tmp_path, s3)
    assert rows(pipe) == {"s3://cognee-s3-test/docs/a.txt": "two"}


def test_downstream_load_failure_replays_pending_package(s3, tmp_path):
    put(s3, "docs/a.txt", "one")
    pipe = run(tmp_path, s3)
    assert rows(pipe) == {"s3://cognee-s3-test/docs/a.txt": "one"}

    put(s3, "docs/a.txt", "two")
    real_load = pipe.load

    def fail_load(*args, **kwargs):
        raise RuntimeError("injected load failure")

    pipe.load = fail_load
    with pytest.raises(RuntimeError, match="injected load failure"):
        pipe.run(
            s3_source(BUCKET, PREFIX, client=s3),
            write_disposition="merge",
            primary_key="id",
        )

    # dlt retained the extracted pending load package. On the next run it loads
    # that package before considering new extraction, so the changed object is
    # not lost even though the previous destination load failed.
    pipe.load = real_load
    retry_source = s3_source(BUCKET, PREFIX, client=s3)
    pipe.run(retry_source, write_disposition="merge", primary_key="id")
    assert rows(pipe) == {"s3://cognee-s3-test/docs/a.txt": "two"}


def test_object_identity_is_url_safe_and_stable(s3, tmp_path):
    key = "docs/A file #1.txt"
    put(s3, key, "content")
    pipe = run(tmp_path, s3)
    assert rows(pipe) == {"s3://cognee-s3-test/docs/A%20file%20%231.txt": "content"}

    s3.delete_object(Bucket=BUCKET, Key=key)
    pipe = run(tmp_path, s3)
    assert rows(pipe) == {}


@pytest.mark.parametrize("bad_key", ["other/outside.txt", None])
def test_invalid_listing_keys_fail_closed(bad_key):
    class BadClient:
        def get_paginator(self, name):
            assert name == "list_objects_v2"

            class Paginator:
                def paginate(self, **kwargs):
                    yield {
                        "Contents": [
                            {
                                "Key": bad_key,
                                "Size": 1,
                                "ETag": '"etag"',
                                "LastModified": "2026-10-16T00:00:00Z",
                            }
                        ]
                    }

            return Paginator()

    with pytest.raises(S3InventoryError, match="invalid object key"):
        _inventory(BadClient(), BUCKET, PREFIX, 10)


def test_duplicate_listing_key_fails_closed():
    class DuplicateClient:
        def get_paginator(self, name):
            assert name == "list_objects_v2"

            class Paginator:
                def paginate(self, **kwargs):
                    obj = {
                        "Key": "docs/a.txt",
                        "Size": 1,
                        "ETag": '"etag"',
                        "LastModified": "2026-10-16T00:00:00Z",
                    }
                    yield {"Contents": [obj, obj]}

            return Paginator()

    with pytest.raises(S3InventoryError, match="duplicate object key"):
        _inventory(DuplicateClient(), BUCKET, PREFIX, 10)


def test_folder_markers_cannot_bypass_object_limit():
    class MarkerClient:
        def get_paginator(self, name):
            assert name == "list_objects_v2"

            class Paginator:
                def paginate(self, **kwargs):
                    yield {
                        "Contents": [
                            {
                                "Key": f"docs/folder-{index}/",
                                "Size": 0,
                                "ETag": '"etag"',
                                "LastModified": "2026-10-16T00:00:00Z",
                            }
                            for index in range(3)
                        ]
                    }

            return Paginator()

    with pytest.raises(S3InventoryError, match="partial reconciliation"):
        _inventory(MarkerClient(), BUCKET, PREFIX, 2)


def test_default_client_uses_boto3_credential_chain(tmp_path):
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket=BUCKET)
    put(client, "docs/a.txt", "credential-chain")

    pipe = pipeline(tmp_path, name="s3_default_client")
    pipe.run(
        s3_source(BUCKET, PREFIX),
        write_disposition="merge",
        primary_key="id",
    )
    assert rows(pipe) == {"s3://cognee-s3-test/docs/a.txt": "credential-chain"}


def test_partial_read_failure_never_publishes_partial_update(s3, tmp_path):
    put(s3, "docs/a.txt", "old-a")
    put(s3, "docs/b.txt", "old-b")
    pipe = run(tmp_path, s3)
    assert rows(pipe) == {
        "s3://cognee-s3-test/docs/a.txt": "old-a",
        "s3://cognee-s3-test/docs/b.txt": "old-b",
    }

    put(s3, "docs/a.txt", "new-a")
    put(s3, "docs/b.txt", "new-b")

    class FailSecondRead:
        def get_paginator(self, name):
            return s3.get_paginator(name)

        def get_object(self, **kwargs):
            if kwargs["Key"] == "docs/b.txt":
                raise PermissionError("second read denied")
            return s3.get_object(**kwargs)

    with pytest.raises(PipelineStepFailed, match="became unreadable"):
        run(tmp_path, FailSecondRead())

    # The first changed object may have been extracted before the second read
    # failed, but the failed extraction must not partially publish it.
    assert rows(pipe) == {
        "s3://cognee-s3-test/docs/a.txt": "old-a",
        "s3://cognee-s3-test/docs/b.txt": "old-b",
    }

    pipe = run(tmp_path, s3)
    assert rows(pipe) == {
        "s3://cognee-s3-test/docs/a.txt": "new-a",
        "s3://cognee-s3-test/docs/b.txt": "new-b",
    }