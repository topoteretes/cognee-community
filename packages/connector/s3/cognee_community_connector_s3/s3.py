"""Bounded, fail-closed S3 document snapshots for Cognee.

A complete snapshot is deliberately used: no partial inventory may authorize
orphan cleanup. No credentials, contents, or object metadata are logged.
"""
from __future__ import annotations

from urllib.parse import quote

TABLE_NAME = "s3_documents"
SOURCE_NAME = "s3"


def snapshot_rows(client, bucket: str, prefix: str = "", *,
                  max_objects: int = 10000, max_bytes: int = 2_000_000,
                  extensions: tuple[str, ...] = (".txt", ".md", ".csv", ".json")):
    """Return complete document rows; raise before publishing on any failure."""
    if not bucket or max_objects < 1 or max_bytes < 1:
        raise ValueError("bucket and positive limits required")
    paginator = client.get_paginator("list_objects_v2")
    objects = []
    seen = set()
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.startswith(prefix) or key in seen:
                raise ValueError("invalid or duplicate S3 listing")
            seen.add(key)
            if len(seen) > max_objects:
                raise ValueError("S3 object limit exceeded; snapshot aborted")
            if key.lower().endswith(extensions):
                if obj["Size"] > max_bytes:
                    raise ValueError("S3 document size limit exceeded; snapshot aborted")
                objects.append(obj)
    rows = []
    for obj in objects:
        key = obj["Key"]
        params = {"Bucket": bucket, "Key": key}
        if obj.get("ETag"):
            params["IfMatch"] = obj["ETag"]
        response = client.get_object(**params)
        body = response["Body"].read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ValueError("S3 document exceeded read limit; snapshot aborted")
        content = body.decode("utf-8")
        rows.append({"id": f"s3://{bucket}/{quote(key, safe='/')}",
                     "title": key.rsplit("/", 1)[-1],
                     "content": content,
                     "url": f"s3://{bucket}/{quote(key, safe='/')}"})
    return rows


def s3_source(bucket: str, prefix: str = "", *, client=None,
              max_objects: int = 10000, max_bytes: int = 2_000_000):
    """Return Cognee document-mode dlt source; boto3 uses its credential chain."""
    try:
        import dlt
        from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
    except ImportError as exc:
        raise ImportError("Install cognee and dlt to use the S3 connector") from exc
    if client is None:
        try:
            import boto3
        except ImportError as exc:
            raise ImportError("Install boto3 to use the S3 connector") from exc
        client = boto3.client("s3")

    @dlt.resource(name=TABLE_NAME, primary_key="id", write_disposition="replace")
    def documents():
        # Materialize the entire inventory and all content before yielding a row.
        # An exception must abort the dlt load; never silently skip errors.
        yield from snapshot_rows(client, bucket, prefix, max_objects=max_objects,
                                 max_bytes=max_bytes)

    @dlt.source(name=SOURCE_NAME)
    def source():
        return documents

    result = source()
    setattr(result, DOCUMENT_SOURCE_ATTR, SOURCE_NAME)
    return result
