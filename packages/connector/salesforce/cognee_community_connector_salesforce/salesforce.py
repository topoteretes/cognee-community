"""Salesforce connector for cognee — a dlt source for structured CRM records.

Syncs standard Salesforce objects (Account, Opportunity, Case, Chatter FeedItem,
FeedComment) into cognee memory incrementally with forget-on-delete.

Unlike text document sources, Salesforce CRM records are ingested as structured
relational tables (`DOCUMENT_SOURCE_ATTR` is not set). Field values, relational
lookups, and system timestamps are preserved in staging tables.

Usage:
    import cognee
    from cognee_community_connector_salesforce import salesforce_source

    await cognee.remember(
        salesforce_source(
            instance_url="https://yourinstance.my.salesforce.com",
            client_id="…",
            client_secret="…",
            refresh_token="…",
            objects=["Account", "Opportunity", "Case", "FeedItem"],
        ),
        dataset_name="salesforce_crm",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator
from typing import Any

try:
    from cognee.shared.logging_utils import get_logger

    logger = get_logger("salesforce_connector")
except ImportError:
    import logging

    logger = logging.getLogger("salesforce_connector")

from .client import SalesforceClient

DEFAULT_ID_PREFIX = "salesforce"

# Standard queries for supported Salesforce objects
OBJECT_QUERIES: dict[str, str] = {
    "Account": (
        "SELECT Id, Name, Type, Industry, AnnualRevenue, Phone, Website, "
        "BillingCity, BillingState, BillingCountry, Description, OwnerId, "
        "CreatedDate, LastModifiedDate FROM Account"
    ),
    "Opportunity": (
        "SELECT Id, Name, AccountId, Amount, StageName, Probability, "
        "CloseDate, Type, LeadSource, Description, OwnerId, "
        "CreatedDate, LastModifiedDate FROM Opportunity"
    ),
    "Case": (
        "SELECT Id, CaseNumber, Subject, Description, Status, Priority, "
        "Origin, AccountId, ContactId, ClosedDate, OwnerId, "
        "CreatedDate, LastModifiedDate FROM Case"
    ),
    "FeedItem": (
        "SELECT Id, ParentId, Type, Title, Body, CreatedById, "
        "CreatedDate, LastModifiedDate FROM FeedItem"
    ),
    "FeedComment": (
        "SELECT Id, FeedItemId, ParentId, CommentBody, CreatedById, "
        "CreatedDate, LastModifiedDate FROM FeedComment"
    ),
}


def _record_to_row(
    obj_type: str,
    record: dict[str, Any],
    instance_url: str,
    id_prefix: str = DEFAULT_ID_PREFIX,
) -> dict[str, Any]:
    """Transform a raw Salesforce record into a structured dlt row."""
    rec_id = str(record["Id"])
    row_id = f"{id_prefix}:{obj_type.lower()}:{rec_id}"

    row: dict[str, Any] = {
        "id": row_id,
        "salesforce_id": rec_id,
        "object_type": obj_type,
        "url": f"{instance_url}/{rec_id}" if instance_url else "",
        "last_modified_date": record.get("LastModifiedDate"),
        "_deleted": False,
    }

    # Add entity fields (skipping Salesforce metadata dictionary 'attributes' and raw 'Id')
    for key, value in record.items():
        if key == "attributes" or key.lower() == "id" or isinstance(value, dict):
            continue

        normalized_key = key.lower()
        row[normalized_key] = value

    # Normalize relational lookup references to global namespaced IDs
    if record.get("AccountId"):
        row["account_id"] = f"{id_prefix}:account:{record['AccountId']}"
    if record.get("ContactId"):
        row["contact_id"] = f"{id_prefix}:contact:{record['ContactId']}"
    if record.get("FeedItemId"):
        row["feed_item_id"] = f"{id_prefix}:feeditem:{record['FeedItemId']}"

    return row


def _deleted_row(
    obj_type: str,
    record_id: str,
    id_prefix: str = DEFAULT_ID_PREFIX,
) -> dict[str, Any]:
    """Emit a hard-delete tombstone row for dlt merge cleanup."""
    return {
        "id": f"{id_prefix}:{obj_type.lower()}:{record_id}",
        "_deleted": True,
    }


def sync_salesforce_object(
    client: Any,
    obj_type: str,
    state: dict[str, Any],
    *,
    id_prefix: str = DEFAULT_ID_PREFIX,
) -> Iterator[dict[str, Any]]:
    """Yield changed records and deletion tombstones for a specific Salesforce object."""
    base_query = OBJECT_QUERIES.get(obj_type)
    if not base_query:
        base_query = f"SELECT Id, LastModifiedDate FROM {obj_type}"

    cursor_key = f"{obj_type}_last_sync"
    last_sync: str | None = state.get(cursor_key)
    now_utc = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    newest_modified = last_sync

    # 1. Fetch changed / new records via SOQL
    if last_sync:
        soql = f"{base_query} WHERE LastModifiedDate >= {last_sync} ORDER BY LastModifiedDate ASC"
    else:
        soql = f"{base_query} ORDER BY LastModifiedDate ASC"

    record_count = 0
    for record in client.query(soql):
        record_count += 1
        lmd = record.get("LastModifiedDate")
        if lmd and (newest_modified is None or lmd > newest_modified):
            newest_modified = lmd

        yield _record_to_row(
            obj_type,
            record,
            getattr(client, "instance_url", ""),
            id_prefix=id_prefix,
        )

    # 2. Fetch deleted records via Salesforce getDeleted API if previous sync exists
    if last_sync and hasattr(client, "get_deleted"):
        try:
            deleted_records = client.get_deleted(obj_type, last_sync, now_utc)
            for del_rec in deleted_records:
                del_id = del_rec.get("id")
                if del_id:
                    yield _deleted_row(obj_type, del_id, id_prefix=id_prefix)
            if deleted_records:
                logger.info(
                    "Salesforce %s: emitted %d tombstone(s).",
                    obj_type,
                    len(deleted_records),
                )
        except Exception as exc:
            logger.warning("Salesforce %s get_deleted check failed: %s", obj_type, exc)

    # 3. Advance incremental high-watermark cursor
    state[cursor_key] = newest_modified or now_utc
    logger.info(
        "Salesforce %s: synced %d record(s). Cursor: %s",
        obj_type,
        record_count,
        state[cursor_key],
    )


def salesforce_source(
    *,
    instance_url: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    refresh_token: str | None = None,
    access_token: str | None = None,
    username: str | None = None,
    password: str | None = None,
    security_token: str | None = None,
    auth_url: str = "https://login.salesforce.com",
    api_version: str = "v60.0",
    objects: list[str] | None = None,
    id_prefix: str = DEFAULT_ID_PREFIX,
    client: Any = None,
):
    """Return a dlt resource that yields structured Salesforce CRM records.

    Args:
        instance_url: Salesforce instance base URL (e.g. 'https://yourinstance.my.salesforce.com').
        client_id: Connected App Consumer Key.
        client_secret: Connected App Consumer Secret.
        refresh_token: OAuth 2.0 refresh token.
        access_token: Direct OAuth 2.0 access token (bypasses auth negotiation).
        username: Salesforce account username.
        password: Salesforce account password.
        security_token: Salesforce user security token.
        auth_url: Identity provider URL ('https://login.salesforce.com' or 'https://test.salesforce.com').
        api_version: Salesforce REST API version (default: 'v60.0').
        objects: List of objects to sync. Defaults to Account, Opportunity, Case, FeedItem, etc.
        id_prefix: Global ID namespace prefix (default: 'salesforce').
        client: Pre-built SalesforceClient instance (for test injection).

    Returns:
        A dlt resource configured with primary_key="id", write_disposition="merge",
        and an _deleted hard-delete column.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            "The Salesforce connector requires dlt. Install with:\n"
            '    pip install "cognee-community-connector-salesforce"'
        ) from exc

    target_objects = objects or ["Account", "Opportunity", "Case", "FeedItem", "FeedComment"]

    @dlt.resource(
        name="salesforce_records",
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def salesforce_records():
        active_client = client or SalesforceClient(
            instance_url=instance_url,
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=refresh_token,
            access_token=access_token,
            username=username,
            password=password,
            security_token=security_token,
            auth_url=auth_url,
            api_version=api_version,
        )

        # Authenticate client if unauthenticated
        if not getattr(active_client, "access_token", None):
            active_client.authenticate()

        resource_state = dlt.current.resource_state()

        for obj in target_objects:
            yield from sync_salesforce_object(
                active_client,
                obj,
                resource_state,
                id_prefix=id_prefix,
            )

    # Note: DOCUMENT_SOURCE_ATTR is NOT set on this resource, ensuring that dlt
    # treats these records as structured relational tables rather than free-form documents.
    return salesforce_records
