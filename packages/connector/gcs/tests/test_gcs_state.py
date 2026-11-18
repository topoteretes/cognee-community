"""Offline integration checks for persistent, scoped GCS version inventories."""

from datetime import UTC, datetime
from unittest.mock import ANY, call, patch

import pytest
from cognee_community_connector_gcs.gcs import TEXT_EXTENSIONS, _scope, gcs_source
from dlt.pipeline.exceptions import PipelineStepFailed
from google.api_core.exceptions import Forbidden, NotFound, PreconditionFailed


def test_incremental_downloads_only_new_and_changed_content(
    client, make_blob, pipeline, rows, run_sync
):
    (a, b) = (make_blob(), make_blob("docs/b.txt"))
    client.list_blobs.return_value = [a, b]
    run_sync(pipeline())
    a.download_as_bytes.reset_mock()
    b.download_as_bytes.reset_mock()
    run_sync(pipeline())
    a.download_as_bytes.assert_not_called()
    b.download_as_bytes.assert_not_called()
    changed = make_blob(generation="654321")
    changed.download_as_bytes.return_value = b"edited"
    added = make_blob("docs/c.txt")
    client.list_blobs.return_value = [changed, b, added]
    run_sync(pipeline())
    changed.download_as_bytes.assert_called_once_with(if_generation_match=654321, retry=ANY)
    added.download_as_bytes.assert_called_once()
    b.download_as_bytes.assert_not_called()
    assert len(rows(pipeline())) == 3
    assert rows(pipeline())["gs://company-documents/docs/a.txt"] == "edited"


def test_metadata_only_change_does_not_download_content(
    client, make_blob, pipeline, rows, run_sync
):
    client.list_blobs.return_value = [make_blob()]
    run_sync(pipeline())
    edited = make_blob(metageneration="2")
    client.list_blobs.return_value = [edited]
    run_sync(pipeline())
    edited.download_as_bytes.assert_not_called()
    assert len(rows(pipeline())) == 1


def test_deletion_tombstones_remove_rows_including_final_object(
    bucket, client, make_blob, pipeline, rows, run_sync
):
    client.list_blobs.return_value = [make_blob(), make_blob("docs/b.txt")]
    run_sync(pipeline())
    client.list_blobs.return_value = [make_blob("docs/b.txt")]
    run_sync(pipeline())
    assert list(rows(pipeline())) == ["gs://company-documents/docs/b.txt"]
    client.list_blobs.return_value = []
    run_sync(pipeline())
    assert rows(pipeline()) == {}
    assert bucket.get_blob.call_args_list == [
        call("docs/a.txt", retry=ANY),
        call("docs/b.txt", retry=ANY),
    ]


def test_replace_downloads_unchanged_rows_instead_of_losing_them(
    client, make_blob, pipeline, rows, run_sync
):
    retained = make_blob()
    client.list_blobs.return_value = [retained, make_blob("docs/b.txt")]
    run_sync(pipeline())
    retained.download_as_bytes.reset_mock()
    client.list_blobs.return_value = [retained]
    run_sync(pipeline(), write_disposition="replace")
    retained.download_as_bytes.assert_called_once()
    assert len(rows(pipeline())) == 1


def test_incomplete_listing_cannot_delete_or_advance_state(
    client, make_blob, pipeline, read_inventories, rows, run_sync
):
    client.list_blobs.return_value = [make_blob()]
    run_sync(pipeline())
    previous = read_inventories(pipeline())

    def failed_pages(*args, **kwargs):
        yield make_blob("docs/b.txt")
        raise RuntimeError("page failed")

    client.list_blobs.side_effect = failed_pages
    with pytest.raises(PipelineStepFailed, match="page failed"):
        run_sync(pipeline())
    assert read_inventories(pipeline()) == previous
    assert len(rows(pipeline())) == 1


def test_object_limit_aborts_before_download_or_state_change(
    client, make_blob, pipeline, read_inventories, run_sync
):
    original = make_blob()
    client.list_blobs.return_value = [original]
    run_sync(pipeline())
    previous = read_inventories(pipeline())
    new = make_blob("docs/new.txt")
    client.list_blobs.return_value = [original, new]
    with pytest.raises(PipelineStepFailed, match="max_objects"):
        run_sync(pipeline(), max_objects=1)
    new.download_as_bytes.assert_not_called()
    assert read_inventories(pipeline()) == previous


@pytest.mark.parametrize("disposition", ["merge", "replace"])
def test_oversized_existing_object_is_removed(
    bucket, client, make_blob, pipeline, read_inventories, rows, run_sync, disposition
):
    client.list_blobs.return_value = [make_blob()]
    run_sync(pipeline())
    oversized = make_blob(generation="654321")
    oversized.size = 100
    client.list_blobs.return_value = [oversized]
    run_sync(pipeline(), write_disposition=disposition, max_object_size=10)
    oversized.download_as_bytes.assert_not_called()
    bucket.get_blob.assert_not_called()
    assert read_inventories(pipeline())["company-documents/docs/"] == {}
    assert rows(pipeline()) == {}


def test_new_oversized_object_is_skipped(client, make_blob, pipeline, rows, run_sync):
    (retained, oversized) = (make_blob(), make_blob("docs/large.txt"))
    oversized.size = 100
    client.list_blobs.return_value = [retained, oversized]
    run_sync(pipeline(), max_object_size=10)
    oversized.download_as_bytes.assert_not_called()
    assert list(rows(pipeline())) == ["gs://company-documents/docs/a.txt"]


def test_object_reappearing_during_delete_check_aborts(
    bucket, client, make_blob, pipeline, read_inventories, rows, run_sync
):
    client.list_blobs.return_value = [make_blob()]
    run_sync(pipeline())
    previous = read_inventories(pipeline())
    client.list_blobs.return_value = []
    bucket.get_blob.return_value = make_blob()
    with pytest.raises(PipelineStepFailed, match="inventory changed"):
        run_sync(pipeline())
    assert read_inventories(pipeline()) == previous
    assert len(rows(pipeline())) == 1


def test_inventory_survives_pipeline_restart_and_records_metadata_changes(
    client, make_blob, pipeline, read_inventories, run_sync
):
    client.list_blobs.return_value = [make_blob()]
    run_sync(pipeline())
    restarted = pipeline()
    scope = _scope("company-documents", "docs/")
    uri = "gs://company-documents/docs/a.txt"
    assert read_inventories(restarted)[scope][uri] == {
        "updated": "2026-10-20T10:00:00Z",
        "generation": "123456",
        "metageneration": "1",
    }
    edited = make_blob(metageneration="2")
    edited.updated = datetime(2026, 10, 20, 11, tzinfo=UTC)
    client.list_blobs.return_value = [edited]
    run_sync(restarted)
    assert read_inventories(pipeline())[scope][uri]["metageneration"] == "2"
    assert read_inventories(pipeline())[scope][uri]["updated"] == "2026-10-20T11:00:00Z"


def test_bucket_and_prefix_keep_independent_cursors(
    client, make_blob, pipeline, read_inventories, run_sync
):
    client.list_blobs.return_value = [make_blob()]
    run_sync(pipeline())
    previous = read_inventories(pipeline()).copy()
    client.list_blobs.return_value = [make_blob("notes/a.txt")]
    pipeline().run(gcs_source("company-documents", "notes/", client=client))
    client.list_blobs.return_value = [make_blob()]
    pipeline().run(gcs_source("other-bucket", "docs/", client=client))
    inventories = read_inventories(pipeline())
    assert len(inventories) == 3
    for scope, inventory in previous.items():
        assert inventories[scope] == inventory


def test_failed_read_does_not_replace_persisted_inventory(
    client, make_blob, pipeline, read_inventories, run_sync
):
    client.list_blobs.return_value = [make_blob()]
    run_sync(pipeline())
    previous = read_inventories(pipeline())
    changed = make_blob(generation="654321")
    changed.download_as_bytes.side_effect = RuntimeError("download failed")
    client.list_blobs.return_value = [changed]
    with pytest.raises(PipelineStepFailed, match="download failed"):
        run_sync(pipeline())
    assert read_inventories(pipeline()) == previous


def test_distinct_dataset_keeps_previous_inventory(
    client, make_blob, pipeline, read_inventories, run_sync
):
    client.list_blobs.return_value = [make_blob()]
    run_sync(pipeline())
    previous = read_inventories(pipeline()).copy()
    run_sync(pipeline("other_dataset"))
    inventories = read_inventories(pipeline("other_dataset"))
    assert inventories == previous
    assert read_inventories(pipeline()) == previous


def test_supported_text_extensions_are_downloaded_case_insensitively(
    client, make_blob, pipeline, rows, run_sync
):
    extensions = {
        "txt",
        "md",
        "markdown",
        "json",
        "csv",
        "xml",
        "yaml",
        "yml",
        "log",
        "html",
        "htm",
        "rst",
    }
    assert extensions == TEXT_EXTENSIONS
    blobs = [make_blob(f"docs/file.{extension.upper()}") for extension in sorted(extensions)]
    client.list_blobs.return_value = blobs
    run_sync(pipeline())
    assert len(rows(pipeline())) == len(extensions)
    for blob in blobs:
        blob.download_as_bytes.assert_called_once()


def test_unsupported_objects_are_skipped_without_download_or_decode(
    client, make_blob, pipeline, rows, run_sync
):
    supported = make_blob()
    skipped = [
        make_blob(name)
        for name in (
            "docs/report.pdf",
            "docs/photo.png",
            "docs/report.docx",
            "docs/archive.zip",
            "docs/README",
            "docs/folder.txt/README",
            "docs/unknown.exe",
        )
    ]
    for blob in skipped:
        blob.download_as_bytes.return_value = b"\xff\xfe"
    client.list_blobs.return_value = [supported, *skipped]
    run_sync(pipeline())
    assert list(rows(pipeline())) == ["gs://company-documents/docs/a.txt"]
    for blob in skipped:
        blob.download_as_bytes.assert_not_called()


def test_unsupported_objects_still_count_toward_object_limit(client, make_blob, pipeline, run_sync):
    blobs = [make_blob(), make_blob("docs/report.pdf")]
    client.list_blobs.return_value = blobs
    with pytest.raises(PipelineStepFailed, match="max_objects"):
        run_sync(pipeline(), max_objects=1)
    for blob in blobs:
        blob.download_as_bytes.assert_not_called()


def test_previously_ingested_unsupported_object_is_removed(
    bucket, client, make_blob, pipeline, read_inventories, rows, run_sync
):
    legacy = make_blob("docs/legacy.dat")
    client.list_blobs.return_value = [legacy, make_blob()]
    with patch("cognee_community_connector_gcs.gcs.TEXT_EXTENSIONS", TEXT_EXTENSIONS | {"dat"}):
        run_sync(pipeline())
    legacy.download_as_bytes.reset_mock()
    run_sync(pipeline())
    legacy.download_as_bytes.assert_not_called()
    bucket.get_blob.assert_not_called()
    assert list(rows(pipeline())) == ["gs://company-documents/docs/a.txt"]
    assert (
        "gs://company-documents/docs/legacy.dat"
        not in read_inventories(pipeline())["company-documents/docs/"]
    )


def test_invalid_utf8_in_supported_file_is_replaced(
    client, make_blob, pipeline, read_inventories, rows, run_sync
):
    client.list_blobs.return_value = [make_blob()]
    run_sync(pipeline())
    changed = make_blob(generation="654321")
    changed.download_as_bytes.return_value = b"\xef\xbb\xbfhello\xff"
    client.list_blobs.return_value = [changed]
    run_sync(pipeline())
    assert rows(pipeline()) == {"gs://company-documents/docs/a.txt": "hello�"}
    assert (
        read_inventories(pipeline())["company-documents/docs/"][
            "gs://company-documents/docs/a.txt"
        ]["generation"]
        == "654321"
    )


def test_object_missing_during_download_is_removed_while_other_rows_load(
    bucket, client, make_blob, pipeline, read_inventories, rows, run_sync
):
    client.list_blobs.return_value = [make_blob()]
    run_sync(pipeline())
    missing = make_blob(generation="654321")
    missing.download_as_bytes.side_effect = NotFound("removed")
    added = make_blob("docs/new.txt")
    client.list_blobs.return_value = [missing, added]
    run_sync(pipeline())
    assert rows(pipeline()) == {"gs://company-documents/docs/new.txt": "hello"}
    assert (
        "gs://company-documents/docs/a.txt"
        not in read_inventories(pipeline())["company-documents/docs/"]
    )
    bucket.get_blob.assert_called_once_with("docs/a.txt", retry=ANY)


def test_new_object_missing_during_download_is_skipped(client, make_blob, pipeline, rows, run_sync):
    missing = make_blob("docs/new.txt")
    missing.download_as_bytes.side_effect = NotFound("removed")
    client.list_blobs.return_value = [make_blob(), missing]
    run_sync(pipeline())
    assert rows(pipeline()) == {"gs://company-documents/docs/a.txt": "hello"}


def test_missing_download_with_reappearing_key_aborts(
    bucket, client, make_blob, pipeline, read_inventories, rows, run_sync
):
    client.list_blobs.return_value = [make_blob()]
    run_sync(pipeline())
    previous = read_inventories(pipeline())
    changed = make_blob(generation="654321")
    changed.download_as_bytes.side_effect = NotFound("generation unavailable")
    client.list_blobs.return_value = [changed]
    bucket.get_blob.return_value = changed
    with pytest.raises(PipelineStepFailed, match="inventory changed"):
        run_sync(pipeline())
    assert read_inventories(pipeline()) == previous
    assert rows(pipeline()) == {"gs://company-documents/docs/a.txt": "hello"}


@pytest.mark.parametrize("error", (Forbidden("denied"), PreconditionFailed("overwritten")))
def test_permission_and_generation_errors_preserve_state(
    client, make_blob, pipeline, read_inventories, rows, run_sync, error
):
    client.list_blobs.return_value = [make_blob()]
    run_sync(pipeline())
    previous = read_inventories(pipeline())
    changed = make_blob(generation="654321")
    changed.download_as_bytes.side_effect = error
    client.list_blobs.return_value = [changed]
    with pytest.raises(PipelineStepFailed):
        run_sync(pipeline())
    assert read_inventories(pipeline()) == previous
    assert rows(pipeline()) == {"gs://company-documents/docs/a.txt": "hello"}
