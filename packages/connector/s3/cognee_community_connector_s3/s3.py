"""AWS S3 document connector with bounded, fail-closed reconciliation."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

SOURCE = "s3"
TEXT_EXTENSIONS = frozenset(
    {"txt", "md", "markdown", "json", "csv", "xml", "yaml", "yml", "log", "html", "htm", "rst"}
)


class S3InventoryError(RuntimeError):
    """The selected S3 scope could not be proven complete and safe to reconcile."""


@dataclass(frozen=True)
class ObjectVersion:
    key: str
    last_modified: str
    etag: str
    size: int

    @property
    def stamp(self) -> str:
        return f"{self.last_modified}|{self.etag}|{self.size}"


def _resource_name(bucket: str, prefix: str) -> str:
    """Return a stable table name unique to one bucket/prefix cleanup scope."""
    digest = hashlib.sha256(f"{bucket}\0{prefix}".encode()).hexdigest()[:32]
    return f"s3_documents_{digest}"


def _identity(bucket: str, key: str) -> str:
    return f"s3://{bucket}/{quote(key, safe='/')}"


def s3_source(
    bucket: str,
    prefix: str,
    *,
    client: Any = None,
    region_name: str | None = None,
    endpoint_url: str | None = None,
    max_objects: int = 1000,
    max_object_size: int = 10 * 1024 * 1024,
    check_active: Callable[[], None] | None = None,
):
    """Return a dlt document resource for one explicit S3 bucket/prefix.

    The connector inventories the entire selected prefix before it emits any
    deletions. Missing objects authorize deletion only after that complete
    inventory succeeds. Changed objects use an ETag precondition on read.

    The dlt resource/table name is derived from bucket+prefix so two S3 scopes
    synchronized into the same Cognee dataset cannot delete each other's data.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError("The S3 connector requires dlt.") from exc

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError(
            "S3 sync requires a Cognee build with table-scoped DLT document cleanup. "
            "Upgrade to Cognee >=1.6.1 before syncing."
        )
    if not isinstance(bucket, str) or not bucket.strip():
        raise ValueError("bucket is required")
    if not isinstance(prefix, str) or not prefix:
        raise ValueError("prefix is required; whole-bucket sync is intentionally refused")
    if type(max_objects) is not int or type(max_object_size) is not int:
        raise ValueError("max_objects and max_object_size must be integers")
    if max_objects < 1 or max_object_size < 1:
        raise ValueError("max_objects and max_object_size must be positive")

    client = client or _client(region_name=region_name, endpoint_url=endpoint_url)
    resource_name = _resource_name(bucket, prefix)
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def objects():
        state = dlt.current.resource_state()
        previous = dict(state.get("objects", {}))
        rows = _iter_rows(
            client,
            bucket,
            prefix,
            previous,
            state,
            stats,
            max_objects=max_objects,
            max_object_size=max_object_size,
            check_active=check_active,
        )
        yield from dlt_utils.guarded_rows(rows, check_active)

    resource = objects()
    setattr(resource, DOCUMENT_SOURCE_ATTR, SOURCE)
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    resource.cognee_sync_stats = stats
    return resource


def _client(*, region_name: str | None, endpoint_url: str | None):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        region_name=region_name or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION"),
        endpoint_url=endpoint_url,
        config=Config(retries={"mode": "standard", "max_attempts": 5}),
    )


def _iter_rows(
    client: Any,
    bucket: str,
    prefix: str,
    previous: dict[str, str],
    state: dict,
    stats: dict[str, int],
    *,
    max_objects: int,
    max_object_size: int,
    check_active: Callable[[], None] | None,
):
    inventory = _inventory(client, bucket, prefix, max_objects, check_active=check_active)
    present = set(inventory)
    current = dict(previous)
    stats.update(
        listed=len(inventory),
        emitted=0,
        unchanged=0,
        skipped=0,
        deleted=0,
    )

    for key, version in inventory.items():
        if check_active is not None:
            check_active()
        if not _supported(key, version.size, max_object_size):
            stats["skipped"] += 1
            continue

        if previous.get(key) == version.stamp:
            current[key] = version.stamp
            stats["unchanged"] += 1
            continue

        text = _read_exact(client, bucket, version, max_object_size)
        yield {
            "id": _identity(bucket, key),
            "title": key.rsplit("/", 1)[-1],
            "content": text,
            "url": _identity(bucket, key),
        }
        current[key] = version.stamp
        stats["emitted"] += 1

    # Only absence from a successfully completed, authoritative inventory can
    # become a hard-delete tombstone.
    for key in sorted(set(previous) - present):
        if check_active is not None:
            check_active()
        yield {"id": _identity(bucket, key), "_deleted": True}
        current.pop(key, None)
        stats["deleted"] += 1

    # dlt persists resource state together with a successful pipeline load.
    # If extraction/load fails, this update is not committed.
    state["objects"] = current


def _inventory(
    client: Any,
    bucket: str,
    prefix: str,
    max_objects: int,
    *,
    check_active: Callable[[], None] | None = None,
) -> dict[str, ObjectVersion]:
    found: dict[str, ObjectVersion] = {}
    seen = 0
    try:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            if check_active is not None:
                check_active()
            if not isinstance(page, dict):
                raise S3InventoryError("invalid S3 listing page")
            for obj in page.get("Contents", []):
                if check_active is not None:
                    check_active()
                key = obj.get("Key")
                if not isinstance(key, str) or not key.startswith(prefix):
                    raise S3InventoryError("invalid object key in S3 listing")
                # Count every listed object, including S3 folder-marker objects.
                # Markers still consume listing work and must not bypass the bound.
                seen += 1
                if seen > max_objects:
                    raise S3InventoryError(
                        f"scope exceeds max_objects={max_objects}; refusing partial reconciliation"
                    )
                if key.endswith("/"):
                    continue
                if key in found:
                    raise S3InventoryError("duplicate object key in S3 listing")
                size = obj.get("Size")
                if type(size) is not int or size < 0:
                    raise S3InventoryError(f"invalid size for S3 object: {key}")
                modified = obj.get("LastModified")
                etag = str(obj.get("ETag") or "").strip('"')
                if not modified or not etag:
                    raise S3InventoryError(f"missing version metadata for S3 object: {key}")
                found[key] = ObjectVersion(
                    key=key,
                    last_modified=(
                        modified.isoformat() if hasattr(modified, "isoformat") else str(modified)
                    ),
                    etag=etag,
                    size=size,
                )
    except S3InventoryError:
        raise
    except Exception as exc:
        raise S3InventoryError("S3 listing failed; no deletion is authorized") from exc
    return found


def _supported(key: str, size: int, max_size: int) -> bool:
    if size > max_size:
        return False
    leaf = key.rsplit("/", 1)[-1]
    return "." in leaf and leaf.rsplit(".", 1)[-1].lower() in TEXT_EXTENSIONS


def _read_exact(client: Any, bucket: str, version: ObjectVersion, max_size: int) -> str:
    try:
        response = client.get_object(Bucket=bucket, Key=version.key, IfMatch=version.etag)
        stream = response["Body"]
        try:
            body = stream.read(max_size + 1)
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()
        if len(body) > max_size:
            raise S3InventoryError(
                f"object exceeded max_object_size after listing: {version.key}; retry the sync"
            )
        return body.decode("utf-8-sig")
    except S3InventoryError:
        raise
    except UnicodeDecodeError as exc:
        raise S3InventoryError(f"object is not valid UTF-8 text: {version.key}") from exc
    except Exception as exc:
        raise S3InventoryError(
            f"object changed or became unreadable after listing: {version.key}; retry the sync"
        ) from exc