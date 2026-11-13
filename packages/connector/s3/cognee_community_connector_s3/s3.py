"""DLT source for AWS S3 objects (LastModified cursor + forget-on-delete).

Lists the objects under a bucket prefix with a ``list_objects_v2`` paginator,
decodes text-like objects, and yields them as a dlt resource for cognee's
ingestion pipeline.

Like the Notion connector, objects are ingested as *normal documents*: the
source declares ``cognee_document_source = "s3"``, so ``resolve_dlt_sources``
tags each row ``external_metadata["source"] = "s3"`` and routes it through the
standard cognify pipeline. Forget-on-delete ends in the same place too: an
object that is no longer in the staging table is removed from the graph and
vector stores by cognee's ``orphan_cleanup``.

Unlike Notion, S3 can list every key cheaply, so the source keeps a per-object
cursor — each key's ``LastModified`` — in dlt resource state. How it is used
depends on the write disposition cognee runs the pipeline with:

* ``merge`` (``cognee.remember(..., write_disposition="merge")``): incremental.
  Only new or changed objects are downloaded and yielded; unchanged ones stay in
  staging untouched. Keys that were in the cursor but are no longer listed are
  yielded as tombstones whose ``hard_delete`` column makes dlt remove the row.
* anything else (cognee's default is ``replace``): full snapshot, exactly like
  Notion — every eligible object is downloaded and staging is rewritten. This
  is the safe fallback: yielding only changed objects under ``replace`` would
  drop every unchanged object from staging and forget it. The cursor is still
  recorded, so a later ``merge`` run starts from an accurate baseline.

Either way unchanged objects keep a stable content-hash ``data_id``, so they are
not re-cognified.
"""

import os
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("s3_connector")

# dlt resource / staging-table name for S3 objects.
S3_TABLE_NAME = "s3_objects"
S3_SOURCE_NAME = "s3"

# Guardrail defaults: a sync over a larger bucket prefix aborts before any
# download, and larger objects are skipped.
DEFAULT_MAX_OBJECTS = 1000
DEFAULT_MAX_OBJECT_SIZE = 10 * 1024 * 1024

# Only objects with these extensions are decoded as UTF-8 text and ingested.
TEXT_EXTENSIONS = frozenset(
    {"txt", "md", "markdown", "json", "csv", "xml", "yaml", "yml", "log", "html", "htm", "rst"}
)

# Retry budget for throttled / transient S3 responses (botocore "standard" mode).
_MAX_RETRIES = 5

# Column whose hard_delete hint makes a tombstone row remove its key on merge.
_DELETED_COLUMN = "deleted"

_EXTRA_HINT = (
    "The S3 connector requires dlt and boto3: "
    'pip install cognee-community-connector-s3 (or pip install "dlt[sqlalchemy]" boto3).'
)


class TooManyObjectsError(ValueError):
    """The bucket prefix holds more objects than ``max_objects`` allows."""


def s3_source(
    bucket: str,
    prefix: str,
    *,
    aws_access_key_id: str | None = None,
    aws_secret_access_key: str | None = None,
    aws_session_token: str | None = None,
    region_name: str | None = None,
    endpoint_url: str | None = None,
    max_objects: int = DEFAULT_MAX_OBJECTS,
    max_object_size: int = DEFAULT_MAX_OBJECT_SIZE,
    client: Any = None,
):
    """Create a dlt source that yields the text objects under an S3 prefix.

    Args:
        bucket: Bucket to read from.
        prefix: Key prefix to scope the sync to (required, e.g. ``"docs/"``).
        aws_access_key_id: IAM access key. Falls back to ``AWS_ACCESS_KEY_ID``.
        aws_secret_access_key: IAM secret key. Falls back to ``AWS_SECRET_ACCESS_KEY``.
        aws_session_token: Optional STS session token. Falls back to
            ``AWS_SESSION_TOKEN``.
        region_name: Bucket region. Falls back to ``AWS_REGION`` /
            ``AWS_DEFAULT_REGION``.
        endpoint_url: Optional S3-compatible endpoint (MinIO, LocalStack, ...).
        max_objects: Abort the sync when the prefix lists more objects than this.
        max_object_size: Skip objects larger than this many bytes.
        client: Pre-built boto3 S3 client (mainly a test-injection point); when
            omitted one is built from the credentials above.

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
        Pass ``write_disposition="merge"`` to cognee for incremental syncs.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    if not bucket:
        raise ValueError("S3 bucket required: pass bucket=.")
    if not prefix:
        # A prefix is required so a sync can never silently cover a whole bucket.
        raise ValueError('S3 prefix required: pass prefix= (e.g. "docs/").')
    if max_objects < 1 or max_object_size < 1:
        raise ValueError("max_objects and max_object_size must be positive.")

    if client is None:
        client = _build_client(
            aws_access_key_id, aws_secret_access_key, aws_session_token, region_name, endpoint_url
        )

    @dlt.resource(
        name=S3_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns={_DELETED_COLUMN: {"data_type": "bool", "hard_delete": True}},
    )
    def s3_objects():
        # cognee passes its own write_disposition to pipeline.run, which
        # overrides the one declared above; read back what this run really uses.
        incremental = holder["resource"].write_disposition == "merge"
        if not incremental:
            logger.info(
                "S3: write_disposition is not 'merge'; running a full snapshot. "
                "Pass write_disposition='merge' to cognee for incremental syncs."
            )

        # Cursor state is namespaced by bucket/prefix: cognee runs every dataset
        # through one dlt pipeline, so resource state is shared between them.
        cursors = dlt.current.resource_state().setdefault("cursors", {})
        previous: dict[str, str] = cursors.get(_scope(bucket, prefix), {})

        # List everything first: a listing error or an over-limit prefix aborts
        # the run before anything is downloaded or forgotten.
        listed = _list_objects(client, bucket, prefix, max_objects)

        current: dict[str, str] = {}
        changed = 0
        for key, obj in listed.items():
            if not _is_eligible(key, obj.get("Size", 0), max_object_size):
                continue
            stamp = _stamp(obj)
            if incremental and previous.get(key) == stamp:
                current[key] = stamp
                continue
            text = _read_object(client, bucket, key)
            if text is None:
                # Deleted between listing and download: treat it as gone.
                continue
            current[key] = stamp
            changed += 1
            yield _object_to_row(bucket, key, text)

        removed = sorted(previous.keys() - current.keys()) if incremental else []
        for key in removed:
            yield {"id": key, _DELETED_COLUMN: True}

        # dlt only commits resource state with a successful load, so a failed
        # run leaves the cursor untouched.
        cursors[_scope(bucket, prefix)] = current
        logger.info(
            "S3: synced s3://%s/%s — %d object(s) ingested, %d unchanged, %d removed.",
            bucket,
            prefix,
            changed,
            len(current) - changed,
            len(removed),
        )

    @dlt.source(name=S3_SOURCE_NAME)
    def _s3():
        return s3_objects

    source = _s3()
    holder = {"resource": source.resources[S3_TABLE_NAME]}
    # Opt into the document ingestion path (object → text document → cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(source, DOCUMENT_SOURCE_ATTR, S3_SOURCE_NAME)
    return source


# ---------------------------------------------------------------------------
# S3 helpers (module-private)
# ---------------------------------------------------------------------------


def _build_client(access_key, secret_key, session_token, region, endpoint_url):
    """Build a boto3 S3 client from explicit arguments or the standard AWS env vars."""
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    access_key = access_key or os.environ.get("AWS_ACCESS_KEY_ID")
    secret_key = secret_key or os.environ.get("AWS_SECRET_ACCESS_KEY")
    session_token = session_token or os.environ.get("AWS_SESSION_TOKEN")
    region = region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")

    if not (access_key and secret_key):
        raise ValueError(
            "AWS credentials required: pass aws_access_key_id= and aws_secret_access_key=, "
            "or set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY."
        )

    return boto3.client(
        "s3",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        aws_session_token=session_token or None,
        region_name=region or None,
        endpoint_url=endpoint_url or None,
        config=Config(retries={"max_attempts": _MAX_RETRIES, "mode": "standard"}),
    )


def _list_objects(client, bucket: str, prefix: str, max_objects: int) -> dict[str, dict]:
    """Return ``{key: object-summary}`` under the prefix, enforcing ``max_objects``.

    "Directory" placeholder keys (ending in ``/``) are ignored. Aborting —
    rather than truncating — is deliberate: a truncated listing would make the
    omitted objects look deleted, and the sync would forget them.
    """
    objects: dict[str, dict] = {}
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            objects[key] = obj
            if len(objects) > max_objects:
                raise TooManyObjectsError(
                    f"s3://{bucket}/{prefix} lists more than max_objects={max_objects} "
                    "objects; narrow the prefix or raise max_objects."
                )
    return objects


def _is_eligible(key: str, size: int, max_object_size: int) -> bool:
    """True when the object is a text type within the size limit."""
    extension = key.rsplit(".", 1)[-1].lower() if "." in key.rsplit("/", 1)[-1] else ""
    if extension not in TEXT_EXTENSIONS:
        logger.debug("S3: skipping %s (unsupported type).", key)
        return False
    if size > max_object_size:
        logger.warning(
            "S3: skipping %s (%d bytes > max_object_size=%d).", key, size, max_object_size
        )
        return False
    return True


def _read_object(client, bucket: str, key: str) -> str | None:
    """Download and decode an object; ``None`` if it vanished since the listing.

    Any other error propagates and aborts the run, leaving staging, the cursor
    and memory untouched.
    """
    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except client.exceptions.NoSuchKey:
        logger.warning("S3: s3://%s/%s disappeared before download, skipping.", bucket, key)
        return None
    with response["Body"] as body:
        return _decode(body.read())


def _decode(data: bytes) -> str:
    """Decode object bytes as UTF-8 (dropping a BOM), replacing invalid bytes."""
    return data.decode("utf-8-sig", errors="replace")


def _object_to_row(bucket: str, key: str, text: str) -> dict:
    """Build the document row for an object.

    Only ``id``/``url``/``title``/``content`` are kept, so a re-upload of
    identical bytes (new ``LastModified``, same text) keeps its content-hash
    data_id and is not re-cognified.
    """
    return {
        "id": key,
        "url": f"s3://{bucket}/{key}",
        "title": key,
        "content": text,
    }


def _stamp(obj: dict) -> str:
    """Cursor value for an object: its LastModified, tie-broken by ETag.

    S3 reports LastModified at one-second resolution, so an overwrite within the
    same second as the previous sync would otherwise look unchanged.
    """
    last_modified = obj["LastModified"]
    if hasattr(last_modified, "isoformat"):
        last_modified = last_modified.isoformat()
    return f"{last_modified}|{obj.get('ETag', '')}"


def _scope(bucket: str, prefix: str) -> str:
    return f"{bucket}/{prefix}"
