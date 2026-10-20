"""Focused checks for storage reads, eligibility, and document construction."""

import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from cognee_community_connector_gcs.gcs import (
    _build_client,
    _decode,
    _is_eligible,
    _list_objects,
    _object_to_row,
    _read_retry,
    _stamp,
)
from google.api_core.exceptions import (
    Forbidden,
    NotFound,
    PreconditionFailed,
    ServiceUnavailable,
    TooManyRequests,
)


class GcsHelperTests(unittest.TestCase):
    def test_client_uses_explicit_credentials_or_adc(self):
        with patch("google.cloud.storage.Client") as factory:
            self.assertIs(_build_client(None), factory.return_value)
            factory.assert_called_once_with()
            self.assertIs(_build_client("key.json"), factory.from_service_account_json.return_value)
            factory.from_service_account_json.assert_called_once_with("key.json")
            factory.from_service_account_json.side_effect = ValueError("invalid credentials")
            with self.assertRaisesRegex(ValueError, "invalid credentials"):
                _build_client("bad.json")
            factory.assert_called_once_with()

    def test_eligibility_boundaries_and_extensionless_names(self):
        self.assertTrue(_is_eligible("docs/NOTE.MD", 1024, 1024))
        self.assertTrue(_is_eligible("docs/empty.txt", 0, 1024))
        self.assertFalse(_is_eligible("docs/large.txt", 1025, 1024))
        self.assertFalse(_is_eligible("docs/plain.pdf", 10, 1024))
        self.assertFalse(_is_eligible("folder.txt/README", 10, 1024))

    def test_decode_bom_unicode_and_invalid_bytes(self):
        self.assertEqual(_decode(b"\xef\xbb\xbfCaf\xc3\xa9"), "Café")
        self.assertEqual(_decode(b"a\xffb"), "a\ufffdb")
        self.assertEqual(_decode(b""), "")

    def test_row_identity_remains_bucket_qualified(self):
        row = _object_to_row("bucket", "docs/a.txt", "hello")
        self.assertEqual(
            row,
            {
                "id": "gs://bucket/docs/a.txt",
                "url": "gs://bucket/docs/a.txt",
                "title": "docs/a.txt",
                "content": "hello",
            },
        )
        self.assertNotEqual(row["id"], _object_to_row("other", "docs/a.txt", "hello")["id"])

    def test_stamp_keeps_generation_and_metadata_separate(self):
        blob = SimpleNamespace(
            updated=datetime(2026, 10, 20, tzinfo=UTC), generation=12, metageneration=3
        )
        self.assertEqual(
            _stamp(blob),
            {"updated": "2026-10-20T00:00:00Z", "generation": "12", "metageneration": "3"},
        )

    def test_listing_ignores_empty_placeholder_and_passes_retry(self):
        client = MagicMock()
        document = SimpleNamespace(name="docs/a.txt", size=5)
        client.list_blobs.return_value = [SimpleNamespace(name="docs/", size=0), document]
        self.assertEqual(_list_objects(client, "bucket", "docs/", 1), {document.name: document})
        self.assertIsNotNone(client.list_blobs.call_args.kwargs["retry"])

    def test_retry_recovers_transient_storage_errors(self):
        operation = MagicMock(
            side_effect=[TooManyRequests("throttled"), ServiceUnavailable("retry"), "ok"]
        )
        retry = _read_retry().with_delay(initial=0, maximum=0)
        self.assertEqual(retry(operation)(), "ok")
        self.assertEqual(operation.call_count, 3)

    def test_retry_does_not_repeat_permission_missing_or_version_errors(self):
        for error in (Forbidden("denied"), NotFound("missing"), PreconditionFailed("changed")):
            with self.subTest(error=type(error).__name__):
                operation = MagicMock(side_effect=error)
                with self.assertRaises(type(error)):
                    _read_retry()(operation)()
                operation.assert_called_once()
