"""AWS S3 document connector with fail-closed scoped reconciliation."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

SOURCE = "s3"
TABLE = "s3_objects"
TEXT_EXTENSIONS = frozenset(
    {"txt", "md", "markdown", "json", "csv", "xml", "yaml", "yml", "log", "html", "htm", "rst"}
)


class S3InventoryError(RuntimeError):
    """The scoped S3 inventory could not be proven complete and safe to reconcile."""


@dataclass(frozen=True)
class ObjectVersion:
    key: str
    last_modified: str
    etag: str
    size: int

    @property
    def stamp(self) -> str:
        return f"{self.last_modified}|{self.etag}"


def s3_source(
    bucket: str,
    prefix: str,
    *,
    client: Any = None,
    region_name: str | None = None,
    endpoint_url: str | None = None,
    max_objects: int = 1000,
    max_object_size: int = 10 * 1024 * 1024,
):
    """Return a dlt document source for one explicit S3 bucket/prefix scope."""
    import dlt

    if not bucket:
        raise ValueError("bucket is required")
    if not prefix:
        raise ValueError("prefix is required; whole-bucket sync is intentionally refused")
    if max_objects < 1 or max_object_size < 1:
        raise ValueError("max_objects and max_object_size must be positive")

    client = client or _client(region_name=region_name, endpoint_url=endpoint_url)

    @dlt.resource(
        name=TABLE,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def objects():
        state = dlt.current.resource_state()
        scopes = state.setdefault("scopes", {})
        scope = f"s3://{bucket}/{prefix}"
        previous: dict[str, str] = dict(scopes.get(scope, {}))

        inventory = _inventory(client, bucket, prefix, max_objects)
        present = set(inventory)
        current = dict(previous)

        for key, version in inventory.items():
            if not _supported(key, version.size, max_object_size):
                # Present-but-skipped is not deletion. Keep any prior checkpoint.
                continue
            if previous.get(key) == version.stamp:
                current[key] = version.stamp
                continue

            text = _read_exact(client, bucket, version)
            yield {
                "id": f"s3://{bucket}/{key}",
                "title": key.rsplit("/", 1)[-1],
                "content": text,
                "url": f"s3://{bucket}/{key}",
            }
            current[key] = version.stamp

        # Only complete authoritative inventory absence can authorize deletion.
        for key in sorted(set(previous) - present):
            yield {"id": f"s3://{bucket}/{key}", "_deleted": True}
            current.pop(key, None)

        scopes[scope] = current

    resource = objects()
    setattr(resource, DOCUMENT_SOURCE_ATTR, SOURCE)
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


def _inventory(client: Any, bucket: str, prefix: str, max_objects: int) -> dict[str, ObjectVersion]:
    found: dict[str, ObjectVersion] = {}
    try:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj.get("Key")
                if not key or key.endswith("/"):
                    continue
                if len(found) >= max_objects:
                    raise S3InventoryError(
                        f"scope exceeds max_objects={max_objects}; refusing partial reconciliation"
                    )
                modified = obj.get("LastModified")
                found[key] = ObjectVersion(
                    key=key,
                    last_modified=(
                        modified.isoformat() if hasattr(modified, "isoformat") else str(modified)
                    ),
                    etag=str(obj.get("ETag") or "").strip('"'),
                    size=int(obj.get("Size") or 0),
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


def _read_exact(client: Any, bucket: str, version: ObjectVersion) -> str:
    try:
        response = client.get_object(Bucket=bucket, Key=version.key, IfMatch=version.etag)
        data = response["Body"].read()
    except Exception as exc:
        raise S3InventoryError(
            f"object changed or became unreadable after listing: {version.key}; retry the sync"
        ) from exc
    return data.decode("utf-8-sig", errors="replace")
