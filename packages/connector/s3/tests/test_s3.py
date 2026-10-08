import io
import pytest

from cognee_community_connector_s3.s3 import snapshot_rows


class FakePaginator:
    def __init__(self, client):
        self.client = client

    def paginate(self, **kwargs):
        if self.client.fail_list:
            raise PermissionError("list denied")
        keys = sorted(k for k in self.client.objects if k.startswith(kwargs["Prefix"]))
        for start in range(0, len(keys), 2):
            yield {"Contents": [{"Key": k, "Size": len(self.client.objects[k]),
                                  "ETag": f'"{len(self.client.objects[k])}"'}
                                 for k in keys[start:start + 2]]}


class FakeS3:
    def __init__(self, objects=None):
        self.objects = objects or {}
        self.fail_list = False
        self.fail_get = False

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return FakePaginator(self)

    def get_object(self, Bucket, Key, IfMatch=None):
        if self.fail_get:
            raise PermissionError("get denied")
        body = self.objects[Key]
        if IfMatch != f'"{len(body)}"':
            raise ValueError("object changed")
        return {"Body": io.BytesIO(body)}


def test_paginated_scoped_snapshot():
    s3 = FakeS3({"a/1.txt": b"one", "a/2.md": b"two", "a/3.txt": b"three",
                 "b/4.txt": b"other"})
    rows = snapshot_rows(s3, "bucket", "a/")
    assert len(rows) == 3
    assert all(row["id"].startswith("s3://bucket/a/") for row in rows)


def test_final_object_deletion_produces_empty_snapshot():
    s3 = FakeS3({"a/1.txt": b"one"})
    assert len(snapshot_rows(s3, "bucket", "a/")) == 1
    s3.objects.clear()
    assert snapshot_rows(s3, "bucket", "a/") == []


@pytest.mark.parametrize("failure", ["fail_list", "fail_get"])
def test_failure_never_returns_partial_snapshot(failure):
    s3 = FakeS3({"a/1.txt": b"one"})
    setattr(s3, failure, True)
    with pytest.raises(PermissionError):
        snapshot_rows(s3, "bucket", "a/")


def test_limits_abort():
    s3 = FakeS3({"a/1.txt": b"one", "a/2.txt": b"two"})
    with pytest.raises(ValueError):
        snapshot_rows(s3, "bucket", "a/", max_objects=1)
    with pytest.raises(ValueError):
        snapshot_rows(s3, "bucket", "a/", max_bytes=2)


def test_noop_and_overwrite():
    s3 = FakeS3({"a/1.txt": b"one"})
    first = snapshot_rows(s3, "bucket", "a/")
    assert first == snapshot_rows(s3, "bucket", "a/")
    s3.objects["a/1.txt"] = b"two"
    assert snapshot_rows(s3, "bucket", "a/")[0]["content"] == "two"
