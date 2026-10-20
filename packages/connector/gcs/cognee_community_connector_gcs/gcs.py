"""Google Cloud Storage documents exposed as a Cognee document-mode dlt source."""

from contextlib import nullcontext
from datetime import UTC
from typing import Any

from cognee.shared.logging_utils import get_logger

logger = get_logger("gcs_connector")

DEFAULT_MAX_OBJECTS = 1000
DEFAULT_MAX_OBJECT_SIZE = 10 * 1024 * 1024


def gcs_source(
    bucket_name: str,
    prefix: str,
    *,
    credentials_path: str | None = None,
    max_objects: int = DEFAULT_MAX_OBJECTS,
    max_object_size: int = DEFAULT_MAX_OBJECT_SIZE,
    client: Any = None,
):
    """Create an incremental source for UTF-8 documents in a GCS bucket.

    An explicit ``credentials_path`` loads a service-account JSON file. Otherwise
    the Google SDK uses Application Default Credentials, including the file named
    by ``GOOGLE_APPLICATION_CREDENTIALS``. Invalid explicit credentials propagate
    rather than falling back to another identity.

    ``prefix`` is required to scope ingestion. ``max_objects`` limits the number
    of non-directory objects listed; exceeding it aborts before any downloads.
    Objects larger than ``max_object_size`` bytes are logged and skipped. Previously
    ingested objects that become oversized are removed from staging on the next sync.
    An optional pre-built ``client`` bypasses credential discovery and remains
    owned by the caller (the connector does not close it).

    Authentication and network reads happen when the source is consumed.
    Every eligible non-directory object in the scope
    must contain UTF-8 text; binary document extraction is not implemented yet.
    Read/decoding failures abort extraction instead of
    publishing a partial snapshot.

    Object version metadata is persisted in dlt resource state after a successful
    load, scoped by bucket and prefix within the dlt pipeline resource state.
    With ``write_disposition="merge"``, only new content generations are downloaded.
    Metadata-only changes update the cursor without downloading the content again.
    Absent objects are rechecked before hard-delete tombstones are emitted. As with
    S3, other write dispositions fall back to full snapshots to retain unchanged rows.
    Pass ``write_disposition="merge"`` to ``cognee.remember`` for incremental sync.
    Use separate dlt pipelines for independent accounts or destination datasets.
    Use the pinned Cognee 1.6.5 with foreground ingestion for final-object
    cleanup. Cognee 1.4.0 skipped graph orphan cleanup on an empty corpus.
    """
    if not bucket_name or not bucket_name.strip():
        raise ValueError("bucket_name must be a non-empty GCS bucket name.")

    if not prefix or not prefix.strip():
        raise ValueError("prefix required: pass prefix= (e.g. `docs/`)")
    if max_objects < 1 or max_object_size < 1:
        raise ValueError("max_objects and max_object_size must be positive.")

    import dlt
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
    from google.cloud import storage

    @dlt.resource(
        name="gcs_documents",
        primary_key="id",
        write_disposition="merge",
        columns={"deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def gcs_documents():
        incremental = holder["resource"].write_disposition == "merge"
        if client is not None:
            storage_client = client
        elif credentials_path is not None:
            storage_client = storage.Client.from_service_account_json(credentials_path)
        else:
            storage_client = storage.Client()

        with nullcontext(storage_client) if client is not None else storage_client:
            scope = _scope(bucket_name, prefix)
            inventories = dlt.current.resource_state().setdefault("inventories", {})
            previous = inventories.get(scope, {})
            objects = []
            for blob in storage_client.list_blobs(bucket_name, prefix=prefix):
                # GCS folders may be represented by empty placeholder objects.
                if blob.name.endswith("/") and blob.size == 0:
                    continue
                objects.append(blob)
                if len(objects) > max_objects:
                    raise ValueError(f"GCS prefix exceeds max_objects={max_objects}.")

            inventory = {}
            oversized = set()
            for blob in objects:
                uri = f"gs://{bucket_name}/{blob.name}"
                if blob.size > max_object_size:
                    logger.warning(
                        "GCS: skipping %s (%d bytes exceeds max_object_size=%d).",
                        uri,
                        blob.size,
                        max_object_size,
                    )
                    oversized.add(uri)
                    continue
                inventory[uri] = {
                    "updated": blob.updated.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                    "generation": str(blob.generation),
                    "metageneration": str(blob.metageneration),
                }
                if incremental and previous.get(uri, {}).get("generation") == str(blob.generation):
                    # Content is unchanged even when updated/metageneration changed.
                    continue
                # Use the live object with a generation precondition. A version-pinned
                # listed blob could still read an old generation in a versioned bucket.
                content = (
                    storage_client.bucket(bucket_name)
                    .blob(blob.name)
                    .download_as_bytes(
                        if_generation_match=int(blob.generation),
                    )
                    .decode("utf-8-sig")
                )
                yield {
                    "id": uri,
                    "url": uri,
                    "title": blob.name,
                    "content": content,
                }
            for uri in sorted(previous.keys() - inventory.keys()):
                name = uri.removeprefix(f"gs://{bucket_name}/")
                if (
                    uri not in oversized
                    and storage_client.bucket(bucket_name).get_blob(name) is not None
                ):
                    raise RuntimeError(f"GCS inventory changed during sync; retry: {uri}")
                if incremental:
                    yield {"id": uri, "deleted": True}
            # Publish only after complete listing and successful extraction. dlt
            # persists resource state with the load; failures must not advance it.
            inventories[scope] = inventory

    @dlt.source(name="gcs")
    def source():
        return gcs_documents

    result = source()
    holder = {"resource": result.resources["gcs_documents"]}
    setattr(result, DOCUMENT_SOURCE_ATTR, "gcs")
    return result


def _scope(bucket_name: str, prefix: str) -> str:
    """Namespace cursor state by bucket and prefix, matching the S3 connector."""
    return f"{bucket_name}/{prefix}"
