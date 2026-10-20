"""Google Cloud Storage documents exposed as a Cognee document-mode dlt source."""

from contextlib import nullcontext
from datetime import UTC
from typing import Any

from cognee.shared.logging_utils import get_logger

logger = get_logger("gcs_connector")

DEFAULT_MAX_OBJECTS = 1000
DEFAULT_MAX_OBJECT_SIZE = 10 * 1024 * 1024

TEXT_EXTENSIONS = frozenset(
    {"txt", "md", "markdown", "json", "csv", "xml", "yaml", "yml", "log", "html", "htm", "rst"}
)


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
    ingested objects that become oversized or have unsupported extensions are removed
    from staging on the next sync. Unsupported objects still count toward ``max_objects``.
    An optional pre-built ``client`` bypasses credential discovery and remains
    owned by the caller (the connector does not close it).

    Authentication and network reads happen when the source is consumed.
    Only objects with supported text extensions (case-insensitive) are downloaded.
    Extensionless and unsupported objects are skipped. Eligible objects must contain
    UTF-8 text; binary document extraction and OCR are not implemented.
    Invalid UTF-8 bytes are replaced. Confirmed missing objects are skipped;
    other read failures abort extraction instead of publishing a partial snapshot.

    Object version metadata is persisted in dlt resource state after a successful
    load, scoped by bucket and prefix within the dlt pipeline resource state.
    With ``write_disposition="merge"``, only new content generations are downloaded.
    Metadata-only changes update the cursor without downloading the content again.
    Absent objects are rechecked before hard-delete tombstones are emitted. Other
    write dispositions fall back to full snapshots to retain unchanged rows.
    Pass ``write_disposition="merge"`` to ``cognee.remember`` for incremental sync.
    Cognee runs each bucket/prefix and destination dataset in its own dlt pipeline,
    so their sync state stays independent.
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
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR

    @dlt.resource(
        name="gcs_documents",
        primary_key="id",
        write_disposition="merge",
        columns={"deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def gcs_documents():
        incremental = holder["resource"].write_disposition == "merge"
        storage_client = client if client is not None else _build_client(credentials_path)
        with nullcontext(storage_client) if client is not None else storage_client:
            scope = _scope(bucket_name, prefix)
            inventories = dlt.current.resource_state().setdefault("inventories", {})
            previous = inventories.get(scope, {})
            listed = _list_objects(storage_client, bucket_name, prefix, max_objects)
            current = {}
            ineligible = set()
            missing = set()
            for name, blob in listed.items():
                uri = f"gs://{bucket_name}/{name}"
                if not _is_eligible(name, blob.size, max_object_size):
                    ineligible.add(uri)
                    continue
                stamp = _stamp(blob)
                if incremental and previous.get(uri, {}).get("generation") == stamp["generation"]:
                    current[uri] = stamp
                    continue
                text = _read_object(storage_client, bucket_name, blob)
                if text is None:
                    missing.add(uri)
                    continue
                current[uri] = stamp
                yield _object_to_row(bucket_name, name, text)

            for uri in sorted(previous.keys() - current.keys()):
                name = uri.removeprefix(f"gs://{bucket_name}/")
                if uri not in ineligible and uri not in missing:
                    _confirm_deleted(storage_client, bucket_name, name)
                if incremental:
                    yield {"id": uri, "deleted": True}
            # Persist the inventory only after complete listing and extraction.
            inventories[scope] = current

    @dlt.source(name="gcs")
    def source():
        return gcs_documents

    result = source()
    holder = {"resource": result.resources["gcs_documents"]}
    setattr(result, DOCUMENT_SOURCE_ATTR, "gcs")
    # Cognee also includes the destination dataset in the pipeline name.
    setattr(result, PIPELINE_SCOPE_ATTR, _scope(bucket_name, prefix))
    return result


def _build_client(credentials_path: str | None):
    """Build a client using explicit service-account credentials or ADC."""
    from google.cloud import storage

    if credentials_path is not None:
        return storage.Client.from_service_account_json(credentials_path)
    return storage.Client()


def _list_objects(client, bucket: str, prefix: str, max_objects: int) -> dict:
    """Complete the prefix listing before downloading or emitting deletions."""
    objects = {}
    for blob in client.list_blobs(bucket, prefix=prefix, retry=_read_retry()):
        if blob.name.endswith("/") and blob.size == 0:
            continue
        objects[blob.name] = blob
        if len(objects) > max_objects:
            raise ValueError(f"GCS prefix exceeds max_objects={max_objects}.")
    return objects


def _is_eligible(name: str, size: int, max_object_size: int) -> bool:
    """Accept supported text extensions within the configured size limit."""
    filename = name.rsplit("/", 1)[-1]
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension not in TEXT_EXTENSIONS:
        logger.debug("GCS: skipping %s (unsupported type).", name)
        return False
    if size > max_object_size:
        logger.warning(
            "GCS: skipping %s (%d bytes exceeds max_object_size=%d).",
            name,
            size,
            max_object_size,
        )
        return False
    return True


def _read_retry():
    """Bound transient Storage read retries; permission/version errors propagate."""
    from google.cloud.storage.retry import DEFAULT_RETRY

    return DEFAULT_RETRY.with_timeout(120.0)


def _read_object(client, bucket: str, blob) -> str | None:
    """Read the listed generation, or return None for a confirmed missing object."""
    from google.api_core.exceptions import NotFound

    try:
        # Read a live blob with a precondition so an overwrite cannot return stale bytes.
        data = (
            client.bucket(bucket)
            .blob(blob.name)
            .download_as_bytes(if_generation_match=int(blob.generation), retry=_read_retry())
        )
    except NotFound:
        # Verify that the key is absent, rather than a generation being unavailable.
        _confirm_deleted(client, bucket, blob.name)
        logger.warning("GCS: gs://%s/%s disappeared before download, skipping.", bucket, blob.name)
        return None
    return _decode(data)


def _decode(data: bytes) -> str:
    """Decode UTF-8 text, dropping a BOM and replacing invalid bytes."""
    return data.decode("utf-8-sig", errors="replace")


def _object_to_row(bucket: str, name: str, text: str) -> dict:
    """Keep bucket-qualified identities stable across existing loads."""
    uri = f"gs://{bucket}/{name}"
    return {"id": uri, "url": uri, "title": name, "content": text}


def _stamp(blob) -> dict[str, str]:
    """Track content generations separately from metadata-only updates."""
    return {
        "updated": blob.updated.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "generation": str(blob.generation),
        "metageneration": str(blob.metageneration),
    }


def _confirm_deleted(client, bucket: str, name: str) -> None:
    """Refuse cleanup when an object missing from the snapshot exists again."""
    if client.bucket(bucket).get_blob(name, retry=_read_retry()) is not None:
        raise RuntimeError(f"GCS inventory changed during sync; retry: gs://{bucket}/{name}")


def _scope(bucket_name: str, prefix: str) -> str:
    """Namespace cursor state by bucket and prefix."""
    return f"{bucket_name}/{prefix}"
