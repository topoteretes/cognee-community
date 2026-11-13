"""Azure Blob Storage documents exposed as a Cognee document-mode dlt source."""

import os
from contextlib import nullcontext
from datetime import UTC
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from cognee.shared.logging_utils import get_logger

logger = get_logger("azure_blob_connector")

AZURE_BLOB_TABLE_NAME = "azure_blob_documents"
AZURE_BLOB_SOURCE_NAME = "azure_blob"
_DELETED_COLUMN = "deleted"

DEFAULT_MAX_OBJECTS = 1000
DEFAULT_MAX_OBJECT_SIZE = 10 * 1024 * 1024

TEXT_EXTENSIONS = frozenset(
    {"txt", "md", "markdown", "json", "csv", "xml", "yaml", "yml", "log", "html", "htm", "rst"}
)


def azure_blob_source(
    container_name: str,
    prefix: str,
    *,
    account_url: str | None = None,
    credentials: str | None = None,
    max_objects: int = DEFAULT_MAX_OBJECTS,
    max_object_size: int = DEFAULT_MAX_OBJECT_SIZE,
    client: Any = None,
):
    """Create an incremental document source for an Azure container prefix.

    Credentials are a SAS token or account key. Explicit values take precedence
    over AZURE_STORAGE_SAS_TOKEN, then AZURE_STORAGE_KEY. The account URL falls
    back to AZURE_STORAGE_ACCOUNT_URL. A caller-supplied BlobServiceClient
    bypasses credential discovery and remains owned by its caller.

    Consume with cognee.remember(..., write_disposition="merge", primary_key="id",
    max_rows_per_table=0, run_in_background=False) for incremental sync and
    downstream deletion cleanup. Other dispositions download a full snapshot;
    append does not synchronize deletions. Use a dedicated dataset per scope.

    Supported text extensions are decoded as UTF-8 (optional BOM), replacing invalid bytes.
    Each run lists the entire prefix before downloading. State is an inventory
    of Last-Modified/ETag pairs, persisted by dlt with successful loads. ETags are
    compared for equality, never ordered. Downloads use an If-Match condition
    against the current blob, rather than retrieving a historical version.

    Authentication, listing, archive-tier and version-race failures
    abort extraction. Missing blobs are confirmed before emitting tombstones;
    blobs still present retain their prior state until the next incremental sync.
    Unsupported or oversized previously ingested blobs also leave memory.
    """
    if not isinstance(container_name, str) or not container_name.strip():
        raise ValueError("container_name must be a non-empty Azure container name.")
    if not isinstance(prefix, str) or not prefix.strip():
        raise ValueError("prefix required: pass prefix= (e.g. `docs/`)")
    if any(type(limit) is not int or limit < 1 for limit in (max_objects, max_object_size)):
        raise ValueError("max_objects and max_object_size must be positive integers.")

    if client is not None:
        resolved_url, resolved_credentials = client.url, None
    else:
        resolved_url = account_url or os.environ.get("AZURE_STORAGE_ACCOUNT_URL")
        if not resolved_url or not resolved_url.strip():
            raise ValueError(
                "Azure storage account URL required: pass account_url= or set "
                "AZURE_STORAGE_ACCOUNT_URL."
            )
        resolved_credentials = (
            credentials
            or os.environ.get("AZURE_STORAGE_SAS_TOKEN")
            or os.environ.get("AZURE_STORAGE_KEY")
        )
        if not resolved_credentials or not resolved_credentials.strip():
            raise ValueError(
                "Azure credentials required: pass credentials= (SAS token or account key), "
                "or set AZURE_STORAGE_SAS_TOKEN or AZURE_STORAGE_KEY."
            )

    # Stable, credential-free identity for sync state.
    parts = urlsplit(resolved_url)
    endpoint = urlunsplit((parts.scheme, parts.netloc.lower(), parts.path.rstrip("/"), "", ""))
    scope = _scope(endpoint, container_name, prefix)

    import dlt
    from azure.core.exceptions import ResourceNotFoundError
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR

    @dlt.resource(
        name=AZURE_BLOB_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns={_DELETED_COLUMN: {"data_type": "bool", "hard_delete": True}},
    )
    def azure_blob_documents():
        incremental = holder["resource"].write_disposition == "merge"
        service = (
            client if client is not None else _build_client(resolved_url, resolved_credentials)
        )
        with nullcontext(service) if client is not None else service as active_service:
            inventories = dlt.current.resource_state().setdefault("inventories", {})
            previous = inventories.get(scope, {})
            container = active_service.get_container_client(container_name)
            # Complete the listing before any download or deletion, like S3.
            objects = _list_objects(container, prefix, max_objects)

            inventory = {}
            ineligible = set()
            base_url = f"{endpoint}/{quote(container_name, safe='')}/"
            for blob in objects.values():
                uri = base_url + quote(blob.name, safe="/")
                if not _is_eligible(blob.name, blob.size, max_object_size):
                    ineligible.add(uri)
                    continue
                cursor = _stamp(blob)
                old = previous.get(uri, {})
                if incremental and all(old.get(key) == value for key, value in cursor.items()):
                    inventory[uri] = {"name": blob.name, **cursor}
                    continue
                text = _read_object(container, blob)
                if text is None:
                    # Vanished after listing: omit it from the current inventory.
                    continue
                inventory[uri] = {"name": blob.name, **cursor}
                yield _object_to_row(uri, blob.name, text)

            if incremental:
                for uri in sorted(previous.keys() - inventory.keys()):
                    if uri not in ineligible:
                        try:
                            container.get_blob_client(previous[uri]["name"]).get_blob_properties()
                        except ResourceNotFoundError as exc:
                            if getattr(exc, "error_code", None) != "BlobNotFound":
                                raise
                        else:
                            # Still exists: retain its state and reconsider next sync.
                            inventory[uri] = previous[uri]
                            continue
                    yield {"id": uri, _DELETED_COLUMN: True}
            # dlt commits the inventory only with successful extraction/load.
            inventories[scope] = inventory

    @dlt.source(name=AZURE_BLOB_SOURCE_NAME)
    def source():
        return azure_blob_documents

    result = source()
    holder = {"resource": result.resources[AZURE_BLOB_TABLE_NAME]}
    setattr(result, DOCUMENT_SOURCE_ATTR, AZURE_BLOB_SOURCE_NAME)
    # Cognee also includes the destination dataset in the pipeline name.
    setattr(result, PIPELINE_SCOPE_ATTR, scope)
    return result


def _build_client(account_url: str, credentials: str | None):
    """Build the Azure service client from the resolved endpoint and credentials."""
    from azure.storage.blob import BlobServiceClient

    return BlobServiceClient(account_url=account_url, credential=credentials)


def _list_objects(container, prefix: str, max_objects: int) -> dict[str, Any]:
    """List the entire prefix, aborting rather than truncating at the object limit."""
    objects = {}
    # ItemPaged consumes all continuation pages, including short intermediate pages.
    for blob in container.list_blobs(name_starts_with=prefix):
        if blob.name.endswith("/") and blob.size == 0:
            continue
        objects[blob.name] = blob
        if len(objects) > max_objects:
            raise ValueError(f"Azure prefix exceeds max_objects={max_objects}.")
    return objects


# Text extraction helpers adapted from cognee-community S3 PR #209,
# commit 8cce7395668dd6ddbe9fe465e5beb4cd676a7209 (s3.py).
# S3 currently keeps these helpers private; keep their behavior here without
# requiring an unreleased connector package at runtime.
def _is_eligible(key: str, size: int, max_object_size: int) -> bool:
    """True when the object is a text type within the size limit."""
    extension = key.rsplit(".", 1)[-1].lower() if "." in key.rsplit("/", 1)[-1] else ""
    if extension not in TEXT_EXTENSIONS:
        logger.debug("Azure Blob: skipping %s (unsupported type).", key)
        return False
    if size > max_object_size:
        logger.warning(
            "Azure Blob: skipping %s (%d bytes > max_object_size=%d).", key, size, max_object_size
        )
        return False
    return True


def _read_object(container, blob) -> str | None:
    """Download and decode a blob; return None only if the blob disappeared."""
    from azure.core import MatchConditions
    from azure.core.exceptions import ResourceNotFoundError

    try:
        # Retain Azure's If-Match protection against overwrites during download.
        payload = (
            container.get_blob_client(blob.name)
            .download_blob(etag=blob.etag, match_condition=MatchConditions.IfNotModified)
            .readall()
        )
    except ResourceNotFoundError as exc:
        if getattr(exc, "error_code", None) != "BlobNotFound":
            raise
        logger.warning("Azure Blob: %s disappeared before download, skipping.", blob.name)
        return None
    return _decode(payload)


def _decode(data: bytes) -> str:
    """Decode object bytes as UTF-8 (dropping a BOM), replacing invalid bytes."""
    return data.decode("utf-8-sig", errors="replace")


def _object_to_row(uri: str, name: str, text: str) -> dict:
    """Build the same id/url/title/content document shape as S3."""
    return {"id": uri, "url": uri, "title": name, "content": text}


def _stamp(blob) -> dict[str, str]:
    """Record Last-Modified and ETag for equality comparisons, never ordering."""
    if not blob.etag or blob.last_modified is None:
        raise ValueError("Azure listing is missing ETag or Last-Modified; retry sync.")
    return {
        "last_modified": blob.last_modified.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "etag": str(blob.etag),
    }


def _scope(endpoint: str, container_name: str, prefix: str) -> str:
    """Namespace cursor state by account, container and prefix, like S3/GCS."""
    return f"{endpoint}/{container_name}/{prefix}"
