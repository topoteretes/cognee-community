"""Focused checks for storage reads, eligibility, and document construction."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
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


def test_client_uses_explicit_credentials_or_adc():
    with patch("google.cloud.storage.Client") as factory:
        assert _build_client(None) is factory.return_value
        factory.assert_called_once_with()
        assert _build_client("key.json") is factory.from_service_account_json.return_value
        factory.from_service_account_json.assert_called_once_with("key.json")
        factory.from_service_account_json.side_effect = ValueError("invalid credentials")
        with pytest.raises(ValueError, match="invalid credentials"):
            _build_client("bad.json")
        factory.assert_called_once_with()


def test_eligibility_boundaries_and_extensionless_names():
    assert _is_eligible("docs/NOTE.MD", 1024, 1024)
    assert _is_eligible("docs/empty.txt", 0, 1024)
    assert not _is_eligible("docs/large.txt", 1025, 1024)
    assert not _is_eligible("docs/plain.pdf", 10, 1024)
    assert not _is_eligible("folder.txt/README", 10, 1024)


def test_decode_bom_unicode_and_invalid_bytes():
    assert _decode(b"\xef\xbb\xbfCaf\xc3\xa9") == "Café"
    assert _decode(b"a\xffb") == "a�b"
    assert _decode(b"") == ""


def test_row_identity_remains_bucket_qualified():
    row = _object_to_row("bucket", "docs/a.txt", "hello")
    assert row == {
        "id": "gs://bucket/docs/a.txt",
        "url": "gs://bucket/docs/a.txt",
        "title": "docs/a.txt",
        "content": "hello",
    }
    assert row["id"] != _object_to_row("other", "docs/a.txt", "hello")["id"]


def test_stamp_keeps_generation_and_metadata_separate():
    blob = SimpleNamespace(
        updated=datetime(2026, 10, 20, tzinfo=UTC), generation=12, metageneration=3
    )
    assert _stamp(blob) == {
        "updated": "2026-10-20T00:00:00Z",
        "generation": "12",
        "metageneration": "3",
    }


def test_listing_ignores_empty_placeholder_and_passes_retry():
    client = MagicMock()
    document = SimpleNamespace(name="docs/a.txt", size=5)
    client.list_blobs.return_value = [SimpleNamespace(name="docs/", size=0), document]
    assert _list_objects(client, "bucket", "docs/", 1) == {document.name: document}
    assert client.list_blobs.call_args.kwargs["retry"] is not None


def test_retry_recovers_transient_storage_errors():
    operation = MagicMock(
        side_effect=[TooManyRequests("throttled"), ServiceUnavailable("retry"), "ok"]
    )
    retry = _read_retry().with_delay(initial=0, maximum=0)
    assert retry(operation)() == "ok"
    assert operation.call_count == 3


@pytest.mark.parametrize(
    "error", (Forbidden("denied"), NotFound("missing"), PreconditionFailed("changed"))
)
def test_retry_does_not_repeat_permission_missing_or_version_errors(error):
    operation = MagicMock(side_effect=error)
    with pytest.raises(type(error)):
        _read_retry()(operation)()
    operation.assert_called_once()
