"""Azure Blob Storage documents exposed as a Cognee document-mode dlt source."""

import os
from contextlib import nullcontext
from datetime import UTC
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from cognee.shared.logging_utils import get_logger

logger = get_logger("azure_blob_connector")

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

    Only supported text extensions containing UTF-8 (optional BOM) are ingested.
    Each run lists the entire prefix before downloading. State is an inventory
    of Last-Modified/ETag pairs, persisted by dlt with successful loads. ETags are
    compared for equality, never ordered. Downloads use an If-Match condition
    against the current blob, rather than retrieving a historical version.

    Authentication, listing, decoding, archive-tier and version-race failures
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
    from azure.core import MatchConditions
    from azure.core.exceptions import ResourceNotFoundError
    from azure.storage.blob import BlobServiceClient
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR

    @dlt.resource(
        name="azure_blob_documents",
        primary_key="id",
        write_disposition="merge",
        columns={"deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def azure_blob_documents():
        incremental = holder["resource"].write_disposition == "merge"
        service = (
            client
            if client is not None
            else BlobServiceClient(account_url=resolved_url, credential=resolved_credentials)
        )
        with nullcontext(service) if client is not None else service as active_service:
            inventories = dlt.current.resource_state().setdefault("inventories", {})
            previous = inventories.get(scope, {})
            container = active_service.get_container_client(container_name)
            objects = []
            # Azure's ItemPaged iterator fetches every continuation page as it is consumed.
            for blob in container.list_blobs(name_starts_with=prefix):
                if blob.name.endswith("/") and blob.size == 0:
                    continue
                objects.append(blob)
                if len(objects) > max_objects:
                    raise ValueError(f"Azure prefix exceeds max_objects={max_objects}.")

            inventory = {}
            ineligible = set()
            base_url = f"{endpoint}/{quote(container_name, safe='')}/"
            for blob in objects:
                uri = base_url + quote(blob.name, safe="/")
                filename = blob.name.rsplit("/", 1)[-1]
                extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
                if extension not in TEXT_EXTENSIONS or blob.size > max_object_size:
                    logger.debug("Azure Blob: skipping an unsupported or oversized blob.")
                    ineligible.add(uri)
                    continue
                if not blob.etag or blob.last_modified is None:
                    raise ValueError("Azure listing is missing ETag or Last-Modified; retry sync.")
                cursor = {
                    "last_modified": blob.last_modified.astimezone(UTC)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "etag": str(blob.etag),
                }
                inventory[uri] = {"name": blob.name, **cursor}
                old = previous.get(uri, {})
                if incremental and all(old.get(key) == value for key, value in cursor.items()):
                    continue
                # If-Match on the current blob catches list/download races even when
                # historical blob versions are enabled on the storage account.
                payload = (
                    container.get_blob_client(blob.name)
                    .download_blob(etag=blob.etag, match_condition=MatchConditions.IfNotModified)
                    .readall()
                )
                yield {
                    "id": uri,
                    "url": uri,
                    "title": blob.name,
                    "content": payload.decode("utf-8-sig"),
                }

            if incremental:
                for uri in sorted(previous.keys() - inventory.keys()):
                    if uri not in ineligible:
                        try:
                            container.get_blob_client(previous[uri]["name"]).get_blob_properties()
                        except ResourceNotFoundError as exc:
                            if exc.error_code != "BlobNotFound":
                                raise
                        else:
                            # Still exists: retain its state and reconsider next sync.
                            inventory[uri] = previous[uri]
                            continue
                    yield {"id": uri, "deleted": True}
            # dlt commits the inventory only with successful extraction/load.
            inventories[scope] = inventory

    @dlt.source(name="azure_blob")
    def source():
        return azure_blob_documents

    result = source()
    holder = {"resource": result.resources["azure_blob_documents"]}
    setattr(result, DOCUMENT_SOURCE_ATTR, "azure_blob")
    # Cognee also includes the destination dataset in the pipeline name.
    setattr(result, PIPELINE_SCOPE_ATTR, scope)
    return result


def _scope(endpoint: str, container_name: str, prefix: str) -> str:
    """Namespace cursor state by account, container and prefix, like S3/GCS."""
    return f"{endpoint}/{container_name}/{prefix}"
