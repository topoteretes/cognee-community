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


def test_incremental_reuses_unchanged_content_and_detects_overwrite():
    class CountingS3(FakeS3):
        reads = 0

        def get_object(self, **kwargs):
            self.reads += 1
            return super().get_object(**kwargs)

    s3 = CountingS3({"a/1.txt": b"one"})
    rows, manifest = snapshot_rows(s3, "bucket", "a/", previous={})
    assert s3.reads == 1
    rows2, manifest2 = snapshot_rows(s3, "bucket", "a/", previous=manifest)
    assert rows2 == rows and s3.reads == 1
    s3.objects["a/1.txt"] = b"longer"
    rows3, manifest3 = snapshot_rows(s3, "bucket", "a/", previous=manifest2)
    assert rows3[0]["content"] == "longer" and s3.reads == 2
    assert manifest3 != manifest2


def test_failed_incremental_run_does_not_mutate_checkpoint():
    s3 = FakeS3({"a/1.txt": b"one"})
    _, manifest = snapshot_rows(s3, "bucket", "a/", previous={})
    original = dict(manifest)
    s3.objects["a/1.txt"] = b"longer"
    s3.fail_get = True
    with pytest.raises(PermissionError):
        snapshot_rows(s3, "bucket", "a/", previous=manifest)
    assert manifest == original


def test_incremental_last_object_deletion():
    s3 = FakeS3({"a/1.txt": b"one"})
    _, manifest = snapshot_rows(s3, "bucket", "a/", previous={})
    s3.objects.clear()
    rows, next_manifest = snapshot_rows(s3, "bucket", "a/", previous=manifest)
    assert rows == [] and next_manifest == {}
    assert manifest


def test_incomplete_listing_fails_closed():
    class TruncatedS3(FakeS3):
        def get_paginator(self, name):
            class Paginator:
                def paginate(self, **kwargs):
                    yield {"IsTruncated": True, "Contents": []}
            return Paginator()
    with pytest.raises(ValueError, match="incomplete"):
        snapshot_rows(TruncatedS3(), "bucket")


def test_scoped_manifest_persistence(tmp_path):
    from cognee_community_connector_s3.s3 import load_manifest, save_manifest, prepare_sync
    path = tmp_path / "checkpoint.json"
    s3 = FakeS3({"a/1.txt": b"one"})
    rows, pending = prepare_sync(s3, "bucket", "a/", path)
    assert not path.exists()  # prepare never advances checkpoint
    save_manifest(path, "bucket", "a/", pending)
    assert load_manifest(path, "bucket", "a/")["s3://bucket/a/1.txt"]["content"] == "one"
    with pytest.raises(ValueError, match="scope"):
        load_manifest(path, "bucket", "b/")
    rows2, _ = prepare_sync(s3, "bucket", "a/", path)
    assert rows2 == rows


def test_corrupt_manifest_fails_closed(tmp_path):
    from cognee_community_connector_s3.s3 import load_manifest
    path = tmp_path / "checkpoint.json"
    path.write_text("{invalid")
    with pytest.raises(ValueError):
        load_manifest(path, "bucket", "a/")


def test_persisted_checkpoint_avoids_download(tmp_path):
    from cognee_community_connector_s3.s3 import prepare_sync, save_manifest

    class CountingS3(FakeS3):
        reads = 0

        def get_object(self, **kwargs):
            self.reads += 1
            return super().get_object(**kwargs)

    s3 = CountingS3({"a/1.txt": b"one"})
    path = tmp_path / "checkpoint.json"
    _, pending = prepare_sync(s3, "bucket", "a/", path)
    save_manifest(path, "bucket", "a/", pending)
    assert s3.reads == 1
    prepare_sync(s3, "bucket", "a/", path)
    assert s3.reads == 1


def test_publication_failure_never_advances_manifest(tmp_path):
    from cognee_community_connector_s3.s3 import sync_with_publisher, load_manifest
    s3 = FakeS3({"a/1.txt": b"one"})
    path = tmp_path / "manifest.json"

    def failing_publisher(rows):
        raise RuntimeError("Cognee ingestion failed")

    with pytest.raises(RuntimeError):
        sync_with_publisher(s3, "bucket", "a/", path, failing_publisher)
    assert not path.exists()
    published = []
    assert sync_with_publisher(s3, "bucket", "a/", path, published.extend) == 1
    assert len(published) == 1
    assert len(load_manifest(path, "bucket", "a/")) == 1


def test_deletion_is_published_before_checkpoint(tmp_path):
    from cognee_community_connector_s3.s3 import sync_with_publisher, load_manifest
    s3 = FakeS3({"a/1.txt": b"one"})
    path = tmp_path / "manifest.json"
    events = []
    sync_with_publisher(s3, "bucket", "a/", path, lambda rows: events.append(len(rows)))
    s3.objects.clear()
    def publish_empty(rows):
        assert rows == []
        assert len(load_manifest(path, "bucket", "a/")) == 1
        events.append(len(rows))
    sync_with_publisher(s3, "bucket", "a/", path, publish_empty)
    assert events == [1, 0]
    assert load_manifest(path, "bucket", "a/") == {}


def test_checkpoint_private_permissions(tmp_path):
    from cognee_community_connector_s3.s3 import save_manifest
    path = tmp_path / "checkpoint.json"
    save_manifest(path, "bucket", "docs/", {})
    assert path.stat().st_mode & 0o077 == 0


def test_checkpoint_symlink_rejected(tmp_path):
    from cognee_community_connector_s3.s3 import load_manifest, save_manifest
    target = tmp_path / "target"
    target.write_text("private")
    link = tmp_path / "manifest.json"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        load_manifest(link, "bucket", "docs/")
    with pytest.raises(ValueError, match="symlink"):
        save_manifest(link, "bucket", "docs/", {})
    assert target.read_text() == "private"
