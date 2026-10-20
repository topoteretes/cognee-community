"""Offline integration checks for persistent, scoped GCS version inventories."""

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, create_autospec

import dlt
from cognee_community_connector_gcs.gcs import _scope, gcs_source
from dlt.pipeline.exceptions import PipelineStepFailed
from google.cloud import storage


class GcsStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.client = create_autospec(storage.Client, instance=True, spec_set=True)
        self.bucket = create_autospec(storage.Bucket, instance=True, spec_set=True)
        self.client.bucket.return_value = self.bucket
        self.bucket.get_blob.return_value = None
        self.bucket.blob.side_effect = lambda name: next(
            b for b in self.client.list_blobs.return_value if b.name == name
        )

    def pipeline(self, dataset="documents"):
        return dlt.pipeline(
            pipeline_name=f"gcs_state_test_{dataset}",
            destination=dlt.destinations.sqlalchemy(f"sqlite:///{self.root / 'documents.db'}"),
            dataset_name=dataset,
            pipelines_dir=str(self.root / "state"),
        )

    def blob(self, name="docs/a.txt", generation="123456", metageneration="1"):
        return SimpleNamespace(
            name=name,
            size=5,
            generation=generation,
            metageneration=metageneration,
            updated=datetime(2026, 10, 20, 10, tzinfo=UTC),
            download_as_bytes=MagicMock(return_value=b"hello"),
        )

    def inventories(self, pipeline):
        return pipeline.state["sources"]["gcs"]["resources"]["gcs_documents"]["inventories"]

    def run_sync(self, pipeline, write_disposition="merge", **kwargs):
        pipeline.run(
            gcs_source("company-documents", "docs/", client=self.client, **kwargs),
            write_disposition=write_disposition,
        )

    def rows(self, pipeline):
        with pipeline.sql_client() as client:
            with client.execute_query("SELECT id, content FROM gcs_documents") as cursor:
                return dict(cursor.fetchall())

    def test_incremental_downloads_only_new_and_changed_content(self):
        a, b = self.blob(), self.blob("docs/b.txt")
        self.client.list_blobs.return_value = [a, b]
        self.run_sync(self.pipeline())
        a.download_as_bytes.reset_mock()
        b.download_as_bytes.reset_mock()
        self.run_sync(self.pipeline())
        a.download_as_bytes.assert_not_called()
        b.download_as_bytes.assert_not_called()
        changed = self.blob(generation="654321")
        changed.download_as_bytes.return_value = b"edited"
        added = self.blob("docs/c.txt")
        self.client.list_blobs.return_value = [changed, b, added]
        self.run_sync(self.pipeline())
        changed.download_as_bytes.assert_called_once_with(if_generation_match=654321)
        added.download_as_bytes.assert_called_once()
        b.download_as_bytes.assert_not_called()
        self.assertEqual(len(self.rows(self.pipeline())), 3)
        self.assertEqual(self.rows(self.pipeline())["gs://company-documents/docs/a.txt"], "edited")

    def test_metadata_only_change_does_not_download_content(self):
        self.client.list_blobs.return_value = [self.blob()]
        self.run_sync(self.pipeline())
        edited = self.blob(metageneration="2")
        self.client.list_blobs.return_value = [edited]
        self.run_sync(self.pipeline())
        edited.download_as_bytes.assert_not_called()
        self.assertEqual(len(self.rows(self.pipeline())), 1)

    def test_deletion_tombstones_remove_rows_including_final_object(self):
        self.client.list_blobs.return_value = [self.blob(), self.blob("docs/b.txt")]
        self.run_sync(self.pipeline())
        self.client.list_blobs.return_value = [self.blob("docs/b.txt")]
        self.run_sync(self.pipeline())
        self.assertEqual(list(self.rows(self.pipeline())), ["gs://company-documents/docs/b.txt"])
        self.client.list_blobs.return_value = []
        self.run_sync(self.pipeline())
        self.assertEqual(self.rows(self.pipeline()), {})
        self.assertEqual(
            self.bucket.get_blob.call_args_list, [call("docs/a.txt"), call("docs/b.txt")]
        )

    def test_replace_downloads_unchanged_rows_instead_of_losing_them(self):
        retained = self.blob()
        self.client.list_blobs.return_value = [retained, self.blob("docs/b.txt")]
        self.run_sync(self.pipeline())
        retained.download_as_bytes.reset_mock()
        self.client.list_blobs.return_value = [retained]
        self.run_sync(self.pipeline(), write_disposition="replace")
        retained.download_as_bytes.assert_called_once()
        self.assertEqual(len(self.rows(self.pipeline())), 1)

    def test_incomplete_listing_cannot_delete_or_advance_state(self):
        self.client.list_blobs.return_value = [self.blob()]
        self.run_sync(self.pipeline())
        previous = self.inventories(self.pipeline())

        def failed_pages(*args, **kwargs):
            yield self.blob("docs/b.txt")
            raise RuntimeError("page failed")

        self.client.list_blobs.side_effect = failed_pages
        with self.assertRaisesRegex(PipelineStepFailed, "page failed"):
            self.run_sync(self.pipeline())
        self.assertEqual(self.inventories(self.pipeline()), previous)
        self.assertEqual(len(self.rows(self.pipeline())), 1)

    def test_object_limit_aborts_before_download_or_state_change(self):
        original = self.blob()
        self.client.list_blobs.return_value = [original]
        self.run_sync(self.pipeline())
        previous = self.inventories(self.pipeline())
        new = self.blob("docs/new.txt")
        self.client.list_blobs.return_value = [original, new]
        with self.assertRaisesRegex(PipelineStepFailed, "max_objects"):
            self.run_sync(self.pipeline(), max_objects=1)
        new.download_as_bytes.assert_not_called()
        self.assertEqual(self.inventories(self.pipeline()), previous)

    def test_oversized_existing_object_emits_tombstone(self):
        self.assert_oversized_removed("merge")

    def test_oversized_existing_object_leaves_replace_snapshot(self):
        self.assert_oversized_removed("replace")

    def assert_oversized_removed(self, disposition):
        self.client.list_blobs.return_value = [self.blob()]
        self.run_sync(self.pipeline())
        oversized = self.blob(generation="654321")
        oversized.size = 100
        self.client.list_blobs.return_value = [oversized]
        self.run_sync(self.pipeline(), write_disposition=disposition, max_object_size=10)
        oversized.download_as_bytes.assert_not_called()
        self.bucket.get_blob.assert_not_called()
        self.assertEqual(self.inventories(self.pipeline())["company-documents/docs/"], {})
        self.assertEqual(self.rows(self.pipeline()), {})

    def test_new_oversized_object_is_skipped(self):
        retained, oversized = self.blob(), self.blob("docs/large.txt")
        oversized.size = 100
        self.client.list_blobs.return_value = [retained, oversized]
        self.run_sync(self.pipeline(), max_object_size=10)
        oversized.download_as_bytes.assert_not_called()
        self.assertEqual(list(self.rows(self.pipeline())), ["gs://company-documents/docs/a.txt"])

    def test_object_reappearing_during_delete_check_aborts(self):
        self.client.list_blobs.return_value = [self.blob()]
        self.run_sync(self.pipeline())
        previous = self.inventories(self.pipeline())
        self.client.list_blobs.return_value = []
        self.bucket.get_blob.return_value = self.blob()
        with self.assertRaisesRegex(PipelineStepFailed, "inventory changed"):
            self.run_sync(self.pipeline())
        self.assertEqual(self.inventories(self.pipeline()), previous)
        self.assertEqual(len(self.rows(self.pipeline())), 1)

    def test_inventory_survives_pipeline_restart_and_records_metadata_changes(self):
        self.client.list_blobs.return_value = [self.blob()]
        self.run_sync(self.pipeline())
        restarted = self.pipeline()
        scope = _scope("company-documents", "docs/")
        uri = "gs://company-documents/docs/a.txt"
        self.assertEqual(
            self.inventories(restarted)[scope][uri],
            {
                "updated": "2026-10-20T10:00:00Z",
                "generation": "123456",
                "metageneration": "1",
            },
        )
        edited = self.blob(metageneration="2")
        edited.updated = datetime(2026, 10, 20, 11, tzinfo=UTC)
        self.client.list_blobs.return_value = [edited]
        self.run_sync(restarted)
        self.assertEqual(self.inventories(self.pipeline())[scope][uri]["metageneration"], "2")
        self.assertEqual(
            self.inventories(self.pipeline())[scope][uri]["updated"], "2026-10-20T11:00:00Z"
        )

    def test_bucket_and_prefix_keep_independent_cursors(self):
        self.client.list_blobs.return_value = [self.blob()]
        self.run_sync(self.pipeline())
        previous = self.inventories(self.pipeline()).copy()
        self.client.list_blobs.return_value = [self.blob("notes/a.txt")]
        self.pipeline().run(gcs_source("company-documents", "notes/", client=self.client))
        self.client.list_blobs.return_value = [self.blob()]
        self.pipeline().run(gcs_source("other-bucket", "docs/", client=self.client))
        inventories = self.inventories(self.pipeline())
        self.assertEqual(len(inventories), 3)
        for scope, inventory in previous.items():
            self.assertEqual(inventories[scope], inventory)

    def test_failed_read_does_not_replace_persisted_inventory(self):
        self.client.list_blobs.return_value = [self.blob()]
        self.run_sync(self.pipeline())
        previous = self.inventories(self.pipeline())
        changed = self.blob(generation="654321")
        changed.download_as_bytes.side_effect = RuntimeError("download failed")
        self.client.list_blobs.return_value = [changed]
        with self.assertRaisesRegex(PipelineStepFailed, "download failed"):
            self.run_sync(self.pipeline())
        self.assertEqual(self.inventories(self.pipeline()), previous)

    def test_distinct_dataset_keeps_previous_inventory(self):
        self.client.list_blobs.return_value = [self.blob()]
        self.run_sync(self.pipeline())
        previous = self.inventories(self.pipeline()).copy()
        self.run_sync(self.pipeline("other_dataset"))
        inventories = self.inventories(self.pipeline("other_dataset"))
        self.assertEqual(inventories, previous)
        self.assertEqual(self.inventories(self.pipeline()), previous)
