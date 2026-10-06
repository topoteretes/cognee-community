"""Unit tests for the S3 dlt connector.

Every test runs inside moto's ``mock_aws`` with fake credentials (see
``conftest.py``) — nothing reaches real AWS. Two layers:

* Helper tests: listing + the ``max_objects`` guardrail, type/size filtering,
  decoding, row building, and the document DataItem tagging (``source="s3"``).
* dlt-pipeline tests (temp sqlite destination, run the way cognee runs it)
  covering the acceptance criteria: first ingest, the LastModified cursor
  re-ingesting only changed objects, and forget-on-delete.
"""

from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

import boto3
import dlt
import pytest
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

from cognee_community_connector_s3.s3 import (
    S3_SOURCE_NAME,
    TooManyObjectsError,
    _build_client,
    _decode,
    _is_eligible,
    _list_objects,
    _object_to_row,
    _read_object,
    s3_source,
)

BUCKET = "test-bucket"
PREFIX = "docs/"

# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


@pytest.fixture
def s3():
    """A moto-backed S3 client with an empty test bucket."""
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket=BUCKET)
    return client


def _put(client, key, body):
    client.put_object(Bucket=BUCKET, Key=key, Body=body.encode() if isinstance(body, str) else body)


class CountingClient:
    """Wraps a boto3 client and records which keys were downloaded."""

    def __init__(self, client):
        self._client = client
        self.downloaded: list[str] = []

    def get_object(self, **kwargs):
        self.downloaded.append(kwargs["Key"])
        return self._client.get_object(**kwargs)

    def __getattr__(self, name):
        return getattr(self._client, name)


def _pipeline(tmp_path):
    db_path = (tmp_path / "s3.db").as_posix()
    return dlt.pipeline(
        pipeline_name="s3_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="s3_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def _run_sync(tmp_path, client, write_disposition="merge", **kwargs):
    """Run s3_source the way cognee's ingest_dlt_source does."""
    run_kwargs = {"write_disposition": write_disposition}
    if write_disposition == "merge":
        run_kwargs["primary_key"] = "id"
    pipeline = _pipeline(tmp_path)
    pipeline.run(s3_source(BUCKET, PREFIX, client=client, **kwargs), **run_kwargs)
    return pipeline


def _read_objects(pipeline):
    """Return {id: content} for the s3_objects staging table."""
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, content FROM s3_objects") as cursor,
    ):
        return {row[0]: row[1] for row in cursor.fetchall()}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_list_objects_scopes_to_prefix_and_skips_directories(s3):
    _put(s3, "docs/a.txt", "a")
    _put(s3, "docs/sub/b.md", "b")
    _put(s3, "docs/sub/", "")  # console-created "folder" placeholder
    _put(s3, "other/c.txt", "c")

    assert set(_list_objects(s3, BUCKET, PREFIX, 10)) == {"docs/a.txt", "docs/sub/b.md"}


def test_list_objects_follows_pagination(s3):
    for i in range(5):
        _put(s3, f"docs/{i}.txt", str(i))
    paginator = s3.get_paginator("list_objects_v2")

    class SmallPages(CountingClient):
        def get_paginator(self, name):
            # Force several pages so the paginator (not one call) is exercised.
            return SimpleNamespace(
                paginate=lambda **kw: paginator.paginate(**kw, PaginationConfig={"PageSize": 2})
            )

    assert len(_list_objects(SmallPages(s3), BUCKET, PREFIX, 10)) == 5


def test_list_objects_aborts_over_max_objects(s3):
    for i in range(3):
        _put(s3, f"docs/{i}.txt", str(i))

    assert len(_list_objects(s3, BUCKET, PREFIX, 3)) == 3
    with pytest.raises(TooManyObjectsError):
        _list_objects(s3, BUCKET, PREFIX, 2)


def test_is_eligible_filters_type_and_size():
    assert _is_eligible("docs/a.md", 10, 100)
    assert _is_eligible("docs/A.TXT", 10, 100)
    assert not _is_eligible("docs/a.pdf", 10, 100)  # unsupported type
    assert not _is_eligible("docs/README", 10, 100)  # no extension
    assert not _is_eligible("docs.v2/README", 10, 100)  # dot only in the "folder"
    assert not _is_eligible("docs/big.txt", 101, 100)  # over the size limit


def test_decode_strips_bom_and_replaces_invalid_bytes():
    assert _decode("﻿hello".encode()) == "hello"
    assert _decode(b"ok \xff") == "ok �"


def test_read_object_returns_none_when_key_vanished(s3):
    _put(s3, "docs/a.txt", "body")
    assert _read_object(s3, BUCKET, "docs/a.txt") == "body"
    assert _read_object(s3, BUCKET, "docs/missing.txt") is None


def test_object_to_row_keeps_only_stable_fields():
    row = _object_to_row(BUCKET, "docs/a.txt", "body")
    assert row == {
        "id": "docs/a.txt",
        "url": "s3://test-bucket/docs/a.txt",
        "title": "docs/a.txt",
        "content": "body",
    }


def test_build_document_data_item_tags_source():
    row = SimpleNamespace(row_data=_object_to_row(BUCKET, "docs/a.txt", "body"), content_hash="h")
    data_id = uuid5(NAMESPACE_OID, "docs/a.txt")

    item = _build_document_data_item(row, data_id, S3_SOURCE_NAME)

    # source="s3" (not "dlt") is what routes the object through normal cognify.
    assert item.external_metadata["source"] == "s3"
    assert item.external_metadata["url"] == "s3://test-bucket/docs/a.txt"
    assert item.external_metadata["external_id"] == "docs/a.txt"
    assert "body" in item.data


def test_s3_source_declares_document_marker(s3):
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    assert document_source_tag(s3_source(BUCKET, PREFIX, client=s3)) == "s3"


def test_s3_source_validates_arguments(s3):
    with pytest.raises(ValueError, match="bucket"):
        s3_source("", PREFIX, client=s3)
    with pytest.raises(ValueError, match="prefix"):
        s3_source(BUCKET, "", client=s3)
    with pytest.raises(ValueError, match="max_objects"):
        s3_source(BUCKET, PREFIX, client=s3, max_objects=0)


def test_build_client_reads_env_credentials(s3):
    _put(s3, "docs/a.txt", "a")
    # conftest sets fake AWS_* env vars; the client built from them hits moto.
    client = _build_client(None, None, None, None, None)
    assert set(_list_objects(client, BUCKET, PREFIX, 10)) == {"docs/a.txt"}


def test_build_client_requires_credentials(monkeypatch):
    monkeypatch.delenv("AWS_ACCESS_KEY_ID")
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY")
    with pytest.raises(ValueError, match="AWS credentials required"):
        _build_client(None, None, None, None, None)


# ---------------------------------------------------------------------------
# dlt pipeline: ingest, incremental cursor, forget-on-delete
# ---------------------------------------------------------------------------


def test_first_sync_ingests_text_objects_under_prefix(s3, tmp_path):
    _put(s3, "docs/a.txt", "alpha body")
    _put(s3, "docs/notes/b.md", "# beta")
    _put(s3, "docs/image.png", b"\x89PNG")  # unsupported type
    _put(s3, "docs/big.txt", "x" * 200)  # over the size limit
    _put(s3, "other/c.txt", "outside the prefix")

    pipeline = _run_sync(tmp_path, s3, max_object_size=100)

    assert _read_objects(pipeline) == {"docs/a.txt": "alpha body", "docs/notes/b.md": "# beta"}


def test_resync_downloads_only_new_or_changed_objects(s3, tmp_path):
    _put(s3, "docs/a.txt", "a v1")
    _put(s3, "docs/b.txt", "b v1")
    first = CountingClient(s3)
    _run_sync(tmp_path, first)
    assert sorted(first.downloaded) == ["docs/a.txt", "docs/b.txt"]

    _put(s3, "docs/a.txt", "a v2")  # changed
    _put(s3, "docs/c.txt", "c v1")  # new
    second = CountingClient(s3)
    pipeline = _run_sync(tmp_path, second)

    # b is unchanged: not downloaded, but still in staging (merge kept it).
    assert sorted(second.downloaded) == ["docs/a.txt", "docs/c.txt"]
    assert _read_objects(pipeline) == {
        "docs/a.txt": "a v2",
        "docs/b.txt": "b v1",
        "docs/c.txt": "c v1",
    }

    third = CountingClient(s3)
    _run_sync(tmp_path, third)
    assert third.downloaded == []  # nothing changed, nothing downloaded


def test_deleted_object_is_removed_on_resync(s3, tmp_path):
    _put(s3, "docs/a.txt", "a")
    _put(s3, "docs/b.txt", "b")
    _run_sync(tmp_path, s3)

    s3.delete_object(Bucket=BUCKET, Key="docs/a.txt")
    pipeline = _run_sync(tmp_path, s3)

    # Absent from staging → cognee's orphan cleanup forgets it downstream.
    assert _read_objects(pipeline) == {"docs/b.txt": "b"}


def test_object_that_becomes_ineligible_is_removed(s3, tmp_path):
    _put(s3, "docs/a.txt", "small")
    _put(s3, "docs/b.txt", "b")
    _run_sync(tmp_path, s3, max_object_size=100)

    _put(s3, "docs/a.txt", "x" * 200)  # now over the size limit
    pipeline = _run_sync(tmp_path, s3, max_object_size=100)

    assert _read_objects(pipeline) == {"docs/b.txt": "b"}


def test_cursor_is_scoped_per_prefix(s3, tmp_path):
    # cognee shares one dlt pipeline across datasets, so a sync of another
    # prefix must neither reuse this prefix's cursor nor tombstone its keys.
    _put(s3, "docs/a.txt", "a")
    _put(s3, "notes/n.txt", "n")
    _run_sync(tmp_path, s3)

    pipeline = _pipeline(tmp_path)
    pipeline.run(
        s3_source(BUCKET, "notes/", client=s3), write_disposition="merge", primary_key="id"
    )
    assert _read_objects(pipeline) == {"docs/a.txt": "a", "notes/n.txt": "n"}

    counting = CountingClient(s3)
    _run_sync(tmp_path, counting)
    assert counting.downloaded == []


def test_replace_disposition_falls_back_to_full_snapshot(s3, tmp_path):
    # cognee's default disposition is replace. Yielding only changed objects
    # there would wipe unchanged ones from staging, so the source must emit a
    # full snapshot — deletions then drop out exactly like the Notion connector.
    _put(s3, "docs/a.txt", "a")
    _put(s3, "docs/b.txt", "b")
    _run_sync(tmp_path, s3, write_disposition="replace")

    s3.delete_object(Bucket=BUCKET, Key="docs/a.txt")
    counting = CountingClient(s3)
    pipeline = _run_sync(tmp_path, counting, write_disposition="replace")

    assert counting.downloaded == ["docs/b.txt"]  # unchanged b re-downloaded
    assert _read_objects(pipeline) == {"docs/b.txt": "b"}


def test_merge_after_replace_uses_recorded_cursor(s3, tmp_path):
    _put(s3, "docs/a.txt", "a")
    _put(s3, "docs/b.txt", "b")
    _run_sync(tmp_path, s3, write_disposition="replace")

    s3.delete_object(Bucket=BUCKET, Key="docs/a.txt")
    counting = CountingClient(s3)
    pipeline = _run_sync(tmp_path, counting)

    # The replace run recorded the cursor: b is not re-downloaded, a is forgotten.
    assert counting.downloaded == []
    assert _read_objects(pipeline) == {"docs/b.txt": "b"}


def test_too_many_objects_aborts_before_download(s3, tmp_path):
    for i in range(3):
        _put(s3, f"docs/{i}.txt", str(i))
    counting = CountingClient(s3)

    with pytest.raises(Exception, match="max_objects"):
        _run_sync(tmp_path, counting, max_objects=2)
    assert counting.downloaded == []


def test_download_error_aborts_and_keeps_cursor(s3, tmp_path):
    # A failed download must abort the run (leaving staging/memory intact) and
    # must not advance the cursor, so the next run retries the object.
    _put(s3, "docs/a.txt", "a v1")
    _run_sync(tmp_path, s3)
    _put(s3, "docs/a.txt", "a v2")

    class Failing(CountingClient):
        def get_object(self, **kwargs):
            raise RuntimeError("download boom")

    with pytest.raises(Exception, match="download boom"):
        _run_sync(tmp_path, Failing(s3))

    counting = CountingClient(s3)
    pipeline = _run_sync(tmp_path, counting)
    assert counting.downloaded == ["docs/a.txt"]
    assert _read_objects(pipeline) == {"docs/a.txt": "a v2"}
