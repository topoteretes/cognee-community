"""Azure connector tests organized like S3's helper and dlt-pipeline suites.

Helper tests exercise listing, extraction, row building and document routing.
Sync tests use Azure SDK doubles with real temporary SQLite dlt destinations
and persistent resource state; they make no live Azure or model API calls.
"""

import copy
import json
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import quote
from uuid import NAMESPACE_OID, uuid5

import dlt
import pytest
from azure.core import MatchConditions
from azure.core.exceptions import HttpResponseError, ResourceModifiedError, ResourceNotFoundError
from azure.storage.blob import BlobClient, BlobServiceClient, ContainerClient
from cognee.tasks.ingestion.dlt_utils import (
    DOCUMENT_SOURCE_ATTR,
    pipeline_name_for_source,
)
from cognee_community_connector_azure_blob import azure_blob_source
from cognee_community_connector_azure_blob.azure_blob import (
    AZURE_BLOB_SOURCE_NAME,
    AZURE_BLOB_TABLE_NAME,
    TEXT_EXTENSIONS,
    _build_client,
    _decode,
    _is_eligible,
    _list_objects,
    _object_to_row,
    _read_object,
    _scope,
    _stamp,
)
from dlt.pipeline.exceptions import PipelineStepFailed

ACCOUNT_URL = "https://example.blob.core.windows.net"


def not_found(code="BlobNotFound"):
    error = ResourceNotFoundError(message="Not found")
    error.error_code = code
    return error


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


class AzureSync:
    def __init__(self, root):
        self.root = root
        self.client = MagicMock(spec=BlobServiceClient)
        self.client.url = ACCOUNT_URL
        self.container = MagicMock(spec=ContainerClient)
        self.client.get_container_client.return_value = self.container
        self.blob_clients = {}
        self.listed = []
        self.container.list_blobs.side_effect = lambda **kw: (
            b for b in self.listed if b.name.startswith(kw["name_starts_with"])
        )
        self.container.get_blob_client.side_effect = self.blob_client

    def blob_client(self, name):
        if name not in self.blob_clients:
            client = MagicMock(spec=BlobClient)
            client.get_blob_properties.side_effect = not_found()
            self.blob_clients[name] = client
        return self.blob_clients[name]

    def blob(self, name="docs/a.txt", etag='"version-1"', payload=b"hello", modified=None):
        client = self.blob_client(name)
        client.download_blob.return_value.readall.return_value = payload
        return SimpleNamespace(
            name=name,
            size=len(payload),
            etag=etag,
            last_modified=modified or datetime(2026, 10, 20, 10, tzinfo=UTC),
        )

    def pipeline(self, dataset="documents"):
        return dlt.pipeline(
            pipeline_name=f"azure_test_{dataset}",
            destination=dlt.destinations.sqlalchemy(f"sqlite:///{self.root / 'documents.db'}"),
            dataset_name=dataset,
            pipelines_dir=str(self.root / "state"),
        )

    def source(self, **kwargs):
        options = {"container_name": "documents", "prefix": "docs/", "client": self.client}
        options.update(kwargs)
        return azure_blob_source(**options)

    def run(self, disposition="merge", dataset="documents", **kwargs):
        result = self.pipeline(dataset).run(self.source(**kwargs), write_disposition=disposition)
        assert not result.has_failed_jobs

    def rows(self, dataset="documents"):
        with (
            self.pipeline(dataset).sql_client() as client,
            client.execute_query("SELECT id, content FROM azure_blob_documents") as cursor,
        ):
            return dict(cursor.fetchall())

    def inventories(self, dataset="documents"):
        state = self.pipeline(dataset).state
        return copy.deepcopy(
            state["sources"]["azure_blob"]["resources"]["azure_blob_documents"]["inventories"]
        )

    def uri(self, name="docs/a.txt"):
        return f"{ACCOUNT_URL}/documents/{quote(name, safe='/')}"

    def downloads(self, name="docs/a.txt"):
        return self.blob_client(name).download_blob


@pytest.fixture
def sync(tmp_path):
    return AzureSync(tmp_path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_list_objects_scopes_to_prefix_and_skips_directories(sync):
    sync.listed = [
        sync.blob(),
        sync.blob("docs/sub/b.md"),
        sync.blob("docs/sub/", payload=b""),
        sync.blob("other/c.txt"),
    ]
    assert set(_list_objects(sync.container, "docs/", 10)) == {"docs/a.txt", "docs/sub/b.md"}


def test_list_objects_follows_pagination(sync):
    from azure.core.paging import ItemPaged

    a, b = sync.blob(), sync.blob("docs/b.txt")
    pages = MagicMock(side_effect=[([a], "next"), ([b], None)])
    sync.container.list_blobs.return_value = ItemPaged(
        pages, lambda response: (response[1], response[0])
    )
    sync.container.list_blobs.side_effect = None
    assert set(_list_objects(sync.container, "docs/", 10)) == {"docs/a.txt", "docs/b.txt"}
    assert pages.call_count == 2


def test_list_objects_aborts_over_max_objects(sync):
    sync.listed = [sync.blob(), sync.blob("docs/b.txt")]
    assert len(_list_objects(sync.container, "docs/", 2)) == 2
    with pytest.raises(ValueError, match="max_objects"):
        _list_objects(sync.container, "docs/", 1)


def test_is_eligible_filters_type_and_size():
    assert _is_eligible("docs/a.md", 10, 100)
    assert _is_eligible("docs/A.TXT", 100, 100)
    assert not _is_eligible("docs/a.pdf", 10, 100)
    assert not _is_eligible("docs/README", 10, 100)
    assert not _is_eligible("docs.v2/README", 10, 100)
    assert not _is_eligible("docs/big.txt", 101, 100)


def test_decode_strips_bom_and_replaces_invalid_bytes():
    assert _decode("\ufeffhello".encode()) == "hello"
    assert _decode(b"ok \xff") == "ok \ufffd"
    assert _decode(b"") == ""


def test_read_object_returns_text_or_none_when_blob_vanished(sync):
    blob = sync.blob(payload=b"\xef\xbb\xbfbody")
    assert _read_object(sync.container, blob) == "body"
    sync.downloads().assert_called_once_with(
        etag=blob.etag, match_condition=MatchConditions.IfNotModified
    )
    sync.downloads().side_effect = not_found()
    assert _read_object(sync.container, blob) is None


def test_object_to_row_keeps_only_stable_fields():
    uri = f"{ACCOUNT_URL}/documents/docs/a.txt"
    assert _object_to_row(uri, "docs/a.txt", "body") == {
        "id": uri,
        "url": uri,
        "title": "docs/a.txt",
        "content": "body",
    }


def test_build_document_data_item_tags_source():
    from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

    uri = f"{ACCOUNT_URL}/documents/docs/a.txt"
    row = SimpleNamespace(
        row_data=_object_to_row(uri, "docs/a.txt", "body"),
        table_name=AZURE_BLOB_TABLE_NAME,
    )
    data_id = uuid5(NAMESPACE_OID, uri)
    item = _build_document_data_item(row, data_id, AZURE_BLOB_SOURCE_NAME)
    # Cognee 1.6.5 stores trusted source/provenance in system_metadata.
    assert item.system_metadata["source"] == "azure_blob"
    assert item.system_metadata["url"] == uri
    assert item.system_metadata["external_id"] == uri
    assert item.data_id == data_id
    assert item.literal_text is True
    assert "body" in item.data


@pytest.mark.parametrize("credential", ["test-account-key", "sv=2023-11-03&sp=rl&sr=c&sig=fake"])
def test_build_client_accepts_account_key_or_sas(credential):
    # Construct real SDK clients without sending requests or using real credentials.
    with _build_client(ACCOUNT_URL, credential) as client:
        assert isinstance(client, BlobServiceClient)
        assert client.url.split("?", 1)[0].rstrip("/") == ACCOUNT_URL
        container = client.get_container_client("documents")
        assert container.url.split("?", 1)[0] == f"{ACCOUNT_URL}/documents"


def test_stamp_normalizes_utc_and_preserves_etag(sync):
    blob = sync.blob(
        etag='"version-0"',
        modified=datetime(2026, 10, 20, 15, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))),
    )
    assert _stamp(blob) == {
        "last_modified": "2026-10-20T10:00:00Z",
        "etag": '"version-0"',
    }


@pytest.mark.parametrize("field", ["etag", "last_modified"])
def test_stamp_rejects_missing_change_metadata(sync, field):
    blob = sync.blob()
    setattr(blob, field, None)
    with pytest.raises(ValueError, match="missing ETag or Last-Modified"):
        _stamp(blob)


def test_document_routing_and_cognee_pipeline_namespace(sync):
    source = sync.source()
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == "azure_blob"
    same = sync.source()
    other_prefix = sync.source(prefix="notes/")
    assert pipeline_name_for_source(source, "a") == pipeline_name_for_source(same, "a")
    names = {
        pipeline_name_for_source(source, "a"),
        pipeline_name_for_source(source, "b"),
        pipeline_name_for_source(other_prefix, "a"),
    }
    assert len(names) == 3
    sync.client.url = "https://other.blob.core.windows.net"
    assert pipeline_name_for_source(sync.source(), "a") not in names


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"container_name": " "}, "container_name"),
        ({"prefix": " "}, "prefix required"),
        ({"max_objects": 0}, "positive integers"),
        ({"max_object_size": 0}, "positive integers"),
        ({"max_objects": True}, "positive integers"),
        ({"max_object_size": 1.5}, "positive integers"),
    ],
)
def test_rejects_invalid_configuration(sync, kwargs, message):
    with pytest.raises(ValueError, match=message):
        sync.source(**kwargs)


@pytest.mark.parametrize(
    ("account_url", "credentials", "message"),
    [(None, "test-key", "account URL required"), (ACCOUNT_URL, None, "credentials required")],
)
def test_missing_configuration_has_helpful_error(account_url, credentials, message):
    with pytest.raises(ValueError, match=message):
        azure_blob_source("documents", "docs/", account_url=account_url, credentials=credentials)


# ---------------------------------------------------------------------------
# dlt pipeline: ingest, incremental cursor, forget-on-delete
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("credentials", "sas_token", "account_key", "expected"),
    [
        ("explicit-key", "env-sas", "env-key", "explicit-key"),
        (None, "env-sas", "env-key", "env-sas"),
        (None, None, "env-key", "env-key"),
    ],
)
def test_credential_precedence_and_owned_client_lifetime(
    sync, monkeypatch, credentials, sas_token, account_key, expected
):
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_URL", ACCOUNT_URL)
    if sas_token:
        monkeypatch.setenv("AZURE_STORAGE_SAS_TOKEN", sas_token)
    if account_key:
        monkeypatch.setenv("AZURE_STORAGE_KEY", account_key)
    sync.listed = [sync.blob()]
    with patch("azure.storage.blob.BlobServiceClient") as constructor:
        constructor.return_value.__enter__.return_value = sync.client
        source = azure_blob_source("documents", "docs/", credentials=credentials)
        constructor.assert_not_called()  # network/client creation is lazy
        sync.pipeline().run(source)
        constructor.assert_called_once_with(account_url=ACCOUNT_URL, credential=expected)
        constructor.return_value.__exit__.assert_called_once()
    assert sync.rows() == {sync.uri(): "hello"}


def test_explicit_url_overrides_environment_and_owned_client_closes_on_failure(sync, monkeypatch):
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_URL", "https://other.blob.core.windows.net")
    sync.container.list_blobs.side_effect = RuntimeError("Listing failed")
    with patch("azure.storage.blob.BlobServiceClient") as constructor:
        constructor.return_value.__enter__.return_value = sync.client
        source = azure_blob_source(
            "documents", "docs/", account_url=ACCOUNT_URL, credentials="test-key"
        )
        with pytest.raises(PipelineStepFailed, match="Listing failed"):
            sync.pipeline().run(source)
        constructor.assert_called_once_with(account_url=ACCOUNT_URL, credential="test-key")
        constructor.return_value.__exit__.assert_called_once()


def test_injected_client_bypasses_configuration_and_remains_open(sync):
    sync.listed = [sync.blob()]
    with patch("azure.storage.blob.BlobServiceClient") as constructor:
        sync.run()
        constructor.assert_not_called()
    sync.client.close.assert_not_called()
    sync.client.__enter__.assert_not_called()
    sync.client.__exit__.assert_not_called()
    sync.client.get_container_client.assert_called_with("documents")


def test_incremental_downloads_only_new_and_changed_blobs(sync):
    sync.listed = [sync.blob(), sync.blob("docs/b.txt")]
    sync.run()
    for name in ("docs/a.txt", "docs/b.txt"):
        sync.downloads(name).reset_mock()
    sync.run()
    sync.downloads().assert_not_called()
    sync.downloads("docs/b.txt").assert_not_called()
    # ETags are opaque, including when lexically smaller at the same timestamp.
    sync.listed = [sync.blob(etag='"version-0"', payload=b"edited"), sync.blob("docs/b.txt")]
    sync.listed.append(sync.blob("docs/c.txt"))
    sync.run()
    sync.downloads().assert_called_once_with(
        etag='"version-0"', match_condition=MatchConditions.IfNotModified
    )
    sync.downloads("docs/b.txt").assert_not_called()
    assert sync.rows() == {
        sync.uri(): "edited",
        sync.uri("docs/b.txt"): "hello",
        sync.uri("docs/c.txt"): "hello",
    }


def test_last_modified_change_with_same_etag_is_not_skipped(sync):
    sync.listed = [sync.blob()]
    sync.run()
    sync.downloads().reset_mock()
    sync.listed = [sync.blob(modified=datetime(2026, 10, 20, 11, tzinfo=UTC))]
    sync.run()
    sync.downloads().assert_called_once()
    cursor = sync.inventories()[_scope(ACCOUNT_URL, "documents", "docs/")][sync.uri()]
    assert cursor["last_modified"] == "2026-10-20T11:00:00Z"


def test_deletions_remove_staging_rows_including_final_blob(sync):
    sync.listed = [sync.blob(), sync.blob("docs/b.txt")]
    sync.run()
    sync.listed = [sync.blob("docs/b.txt")]
    sync.run()
    assert sync.rows() == {sync.uri("docs/b.txt"): "hello"}
    sync.listed = []
    sync.run()
    assert sync.rows() == {}
    assert sync.inventories()[_scope(ACCOUNT_URL, "documents", "docs/")] == {}
    sync.blob_client("docs/a.txt").get_blob_properties.assert_called_once()
    sync.blob_client("docs/b.txt").get_blob_properties.assert_called_once()


@pytest.mark.parametrize("disposition", ["replace", "append"])
def test_nonmerge_downloads_unchanged_content_as_snapshot(sync, disposition):
    sync.listed = [sync.blob()]
    sync.run()
    sync.downloads().reset_mock()
    sync.run(disposition=disposition)
    sync.downloads().assert_called_once()
    assert sync.rows() == {sync.uri(): "hello"}


def test_merge_after_replace_uses_recorded_cursor(sync):
    sync.listed = [sync.blob(), sync.blob("docs/b.txt")]
    sync.run(disposition="replace")
    previous = sync.inventories()
    for name in ("docs/a.txt", "docs/b.txt"):
        sync.downloads(name).reset_mock()
    sync.listed = [sync.blob("docs/b.txt")]
    sync.run(disposition="merge")
    sync.downloads().assert_not_called()
    sync.downloads("docs/b.txt").assert_not_called()
    assert sync.rows() == {sync.uri("docs/b.txt"): "hello"}
    scope = _scope(ACCOUNT_URL, "documents", "docs/")
    assert sync.inventories()[scope] == {
        sync.uri("docs/b.txt"): previous[scope][sync.uri("docs/b.txt")]
    }


@pytest.mark.parametrize("disposition", ["merge", "replace"])
def test_previously_ingested_oversized_blob_leaves_staging(sync, disposition):
    sync.listed = [sync.blob()]
    sync.run()
    sync.listed = [sync.blob(etag='"new"', payload=b"x" * 11)]
    sync.downloads().reset_mock()
    sync.run(disposition=disposition, max_object_size=10)
    sync.downloads().assert_not_called()
    sync.blob_client("docs/a.txt").get_blob_properties.assert_not_called()
    assert sync.rows() == {}


def test_size_boundary_empty_unicode_and_directory_placeholder(sync):
    sync.listed = [
        sync.blob("docs/limit.txt", payload=b"x" * 10),
        sync.blob("docs/large.txt", payload=b"x" * 11),
        sync.blob("docs/empty.txt", payload=b""),
        sync.blob("docs/unicode.txt", payload="\ufeffhi\u2603".encode()),
        sync.blob("docs/folder/", payload=b""),
    ]
    sync.run(max_object_size=10, max_objects=4)
    assert sync.rows() == {
        sync.uri("docs/limit.txt"): "x" * 10,
        sync.uri("docs/empty.txt"): "",
        sync.uri("docs/unicode.txt"): "hi\u2603",
    }
    sync.downloads("docs/large.txt").assert_not_called()
    sync.downloads("docs/folder/").assert_not_called()


def test_all_text_extensions_case_insensitive_and_unsupported_not_downloaded(sync):
    assert {
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
    } == TEXT_EXTENSIONS
    supported = [sync.blob(f"docs/file.{ext.upper()}") for ext in sorted(TEXT_EXTENSIONS)]
    skipped = [
        sync.blob(f"docs/{name}", payload=b"\xff")
        for name in (
            "image.png",
            "report.pdf",
            "report.docx",
            "archive.zip",
            "README",
            "dir.txt/README",
        )
    ]
    sync.listed = [*supported, *skipped]
    sync.run()
    assert len(sync.rows()) == len(TEXT_EXTENSIONS)
    for blob in skipped:
        sync.downloads(blob.name).assert_not_called()


def test_previously_ingested_unsupported_blob_is_removed(sync):
    sync.listed = [sync.blob("docs/legacy.dat"), sync.blob()]
    with patch(
        "cognee_community_connector_azure_blob.azure_blob.TEXT_EXTENSIONS",
        TEXT_EXTENSIONS | {"dat"},
    ):
        sync.run()
    sync.downloads("docs/legacy.dat").reset_mock()
    sync.run()
    sync.downloads("docs/legacy.dat").assert_not_called()
    assert sync.rows() == {sync.uri(): "hello"}


def test_object_limit_counts_unsupported_and_aborts_before_any_download(sync):
    sync.listed = [sync.blob()]
    sync.run()
    previous = sync.inventories()
    sync.downloads().reset_mock()
    sync.listed.append(sync.blob("docs/report.pdf"))
    with pytest.raises(PipelineStepFailed, match="max_objects"):
        sync.run(max_objects=1)
    sync.downloads().assert_not_called()
    assert sync.inventories() == previous
    assert sync.rows() == {sync.uri(): "hello"}


def test_incomplete_paginated_listing_cannot_advance_state_or_delete(sync):
    sync.listed = [sync.blob()]
    sync.run()
    previous = sync.inventories()
    sync.downloads().reset_mock()

    def pages(**kwargs):
        yield sync.blob("docs/new.txt")
        raise HttpResponseError("second page failed")

    sync.container.list_blobs.side_effect = pages
    with pytest.raises(PipelineStepFailed, match="second page failed"):
        sync.run()
    sync.downloads("docs/new.txt").assert_not_called()
    assert sync.inventories() == previous
    assert sync.rows() == {sync.uri(): "hello"}


@pytest.mark.parametrize("failure", ["overwrite", "expired-sas", "archive-tier"])
def test_failed_download_preserves_persisted_state_and_rows(sync, failure):
    sync.listed = [sync.blob()]
    sync.run()
    previous = sync.inventories()
    sync.listed = [sync.blob(etag='"new"')]
    error = (
        ResourceModifiedError("ETag mismatch")
        if failure == "overwrite"
        else HttpResponseError(failure)
    )
    sync.downloads().side_effect = error
    with pytest.raises(PipelineStepFailed):
        sync.run()
    assert sync.inventories() == previous
    assert sync.rows() == {sync.uri(): "hello"}


def test_reappearing_blob_is_retained_and_updated_on_next_sync(sync):
    sync.listed = [sync.blob()]
    sync.run()
    previous = sync.inventories()
    sync.downloads().reset_mock()
    sync.listed = []
    sync.blob_client("docs/a.txt").get_blob_properties.side_effect = None
    sync.run()
    sync.blob_client("docs/a.txt").get_blob_properties.assert_called_once()
    sync.downloads().assert_not_called()
    assert sync.rows() == {sync.uri(): "hello"}
    assert sync.inventories() == previous

    sync.listed = [sync.blob(etag='"new"', payload=b"updated")]
    sync.run()
    sync.downloads().assert_called_once()
    assert sync.rows() == {sync.uri(): "updated"}


@pytest.mark.parametrize("failure", ["ContainerNotFound", "generic-404", "authorization"])
def test_failed_deletion_check_preserves_state_and_rows(sync, failure):
    sync.listed = [sync.blob()]
    sync.run()
    previous = sync.inventories()
    sync.listed = []
    error = (
        HttpResponseError("SAS expired")
        if failure == "authorization"
        else not_found(None if failure == "generic-404" else failure)
    )
    sync.blob_client("docs/a.txt").get_blob_properties.side_effect = error
    with pytest.raises(PipelineStepFailed):
        sync.run()
    assert sync.inventories() == previous
    assert sync.rows() == {sync.uri(): "hello"}


def test_partial_extraction_is_not_loaded_and_retry_recovers(sync):
    sync.listed = [sync.blob()]
    sync.run()
    previous = sync.inventories()
    # The first new row has been yielded when the second blob's read fails.
    sync.listed = [sync.blob("docs/new.txt"), sync.blob(etag='"new"', payload=b"updated")]
    sync.downloads().side_effect = ResourceModifiedError("ETag mismatch")
    with pytest.raises(PipelineStepFailed, match="ETag mismatch"):
        sync.run()
    assert sync.inventories() == previous
    assert sync.rows() == {sync.uri(): "hello"}
    sync.downloads().side_effect = None
    sync.run()
    assert sync.rows() == {sync.uri(): "updated", sync.uri("docs/new.txt"): "hello"}


def test_separate_dlt_destinations_ingest_unchanged_blob_independently(sync):
    sync.listed = [sync.blob()]
    sync.run()
    sync.downloads().reset_mock()
    sync.run(dataset="other_dataset")
    sync.downloads().assert_called_once()
    assert sync.rows() == {sync.uri(): "hello"}
    assert sync.rows("other_dataset") == {sync.uri(): "hello"}


def test_successful_listing_pages_and_prefix_selection(sync):
    from azure.core.paging import ItemPaged

    a, b = sync.blob(), sync.blob("docs/b.txt")
    outside = sync.blob("outside.txt")
    sync.listed = [a, b, outside]
    pages = MagicMock(side_effect=[([a], "next"), ([b], None)])

    def list_pages(**kwargs):
        assert kwargs == {"name_starts_with": "docs/"}
        return ItemPaged(pages, lambda response: (response[1], response[0]))

    sync.container.list_blobs.side_effect = list_pages
    sync.run()
    assert pages.call_count == 2
    assert sync.rows() == {sync.uri(): "hello", sync.uri("docs/b.txt"): "hello"}
    sync.downloads("outside.txt").assert_not_called()


def test_inventory_survives_restart_and_scopes_do_not_collide(sync):
    sync.listed = [sync.blob()]
    sync.run()
    previous = sync.inventories()
    sync.downloads().reset_mock()
    sync.run()
    sync.downloads().assert_not_called()
    assert sync.inventories() == previous
    sync.listed = [sync.blob("notes/a.txt")]
    sync.run(prefix="notes/")
    sync.client.url = "https://other.blob.core.windows.net"
    sync.listed = [sync.blob()]
    sync.run()
    inventories = sync.inventories()
    assert len(inventories) == 3
    for scope, inventory in previous.items():
        assert inventories[scope] == inventory


def test_sas_not_stored_in_rows_or_inventory_and_blob_names_are_escaped(sync):
    sync.client.url = ACCOUNT_URL + "/?sig=SECRET&sp=rl"
    name = "docs/a ?#\u2603.txt"
    sync.listed = [sync.blob(name)]
    sync.run()
    rows = sync.rows()
    assert rows == {sync.uri(name): "hello"}
    assert "SECRET" not in json.dumps(sync.inventories())
    assert "SECRET" not in json.dumps(rows)


@pytest.mark.parametrize("payload", [b"hello \xff", b"\xef\xbb\xbfhello \xff"])
def test_invalid_utf8_is_replaced_like_s3(sync, payload):
    sync.listed = [sync.blob(payload=payload)]
    sync.run()
    assert sync.rows() == {sync.uri(): "hello \ufffd"}
    sync.downloads().reset_mock()
    sync.run()
    sync.downloads().assert_not_called()


@pytest.mark.parametrize("stage", ["request", "readall"])
@pytest.mark.parametrize("previously_imported", [True, False])
def test_blob_disappearing_during_download_does_not_abort_sync(sync, stage, previously_imported):
    sync.listed = [sync.blob("docs/keep.txt")]
    if previously_imported:
        sync.listed.append(sync.blob())
    sync.run()
    sync.listed = [
        sync.blob("docs/keep.txt"),
        sync.blob(etag='"new"'),
        sync.blob("docs/new.txt", payload=b"new document"),
    ]
    downloader = sync.downloads()
    if stage == "request":
        downloader.side_effect = not_found()
    else:
        downloader.return_value.readall.side_effect = not_found()
    sync.run()
    assert sync.rows() == {
        sync.uri("docs/keep.txt"): "hello",
        sync.uri("docs/new.txt"): "new document",
    }
    assert sync.uri() not in sync.inventories()[_scope(ACCOUNT_URL, "documents", "docs/")]


@pytest.mark.parametrize("code", ["ContainerNotFound", None, "AuthorizationFailure"])
def test_other_not_found_errors_during_download_preserve_state(sync, code):
    sync.listed = [sync.blob()]
    sync.run()
    previous = sync.inventories()
    sync.listed = [sync.blob(etag='"new"')]
    sync.downloads().side_effect = not_found(code)
    with pytest.raises(PipelineStepFailed):
        sync.run()
    assert sync.inventories() == previous
    assert sync.rows() == {sync.uri(): "hello"}
