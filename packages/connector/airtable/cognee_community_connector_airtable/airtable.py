"""Full Airtable reconciliation with incremental document emission.

The entire selected inventory is read before publishing a delta. Modified-time
watermarks are persisted alongside canonical document hashes: timestamps alone
cannot see comments, computed values, or records restored below the watermark.
State belongs to dlt; the destination commits it together with the staged rows.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR

if TYPE_CHECKING:
    from dlt.extract.resource import DltResource

logger = get_logger("airtable_connector")
_API = "https://api.airtable.com/v0"
_REQUEST_INTERVAL = 0.21
_MAX_ATTEMPTS = 5
_IDENTIFIER = re.compile(r"^[A-Za-z0-9]+$")
_ATTACHMENT_KEYS = ("id", "filename", "size", "type", "width", "height")


class AirtableError(RuntimeError):
    """A failed or incomplete upstream read; existing staging must be retained."""


def _identifier(value: Any, kind: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"Airtable {kind} must be a non-empty alphanumeric ID.")
    return value


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _fingerprint(row: dict) -> str:
    return hashlib.sha256(_json(row).encode()).hexdigest()


class _Client:
    """A paced, bounded HTTP reader used for one complete base inventory."""

    def __init__(self, session: Any, token: str | None = None):
        self.session = session
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.next_request = 0.0

    def get(self, path: str, **params: Any) -> dict:
        for attempt in range(_MAX_ATTEMPTS):
            delay = self.next_request - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self.next_request = time.monotonic() + _REQUEST_INTERVAL
            try:
                response = self.session.get(
                    f"{_API}/{path}", params=params, timeout=30, headers=self.headers
                )
            except (requests.Timeout, requests.ConnectionError):
                if attempt == _MAX_ATTEMPTS - 1:
                    raise AirtableError(f"Airtable request failed for {path}.") from None
                time.sleep(float(2**attempt))
                continue
            status = response.status_code
            if status == 429 or 500 <= status < 600:
                if attempt == _MAX_ATTEMPTS - 1:
                    raise AirtableError(f"Airtable {path} failed with HTTP {status}.")
                delay = 30.0 if status == 429 else float(2**attempt)
                try:
                    retry_after = float(response.headers.get("Retry-After", ""))
                    if math.isfinite(retry_after) and retry_after >= 0:
                        delay = max(delay, retry_after)
                except (TypeError, ValueError):
                    pass
                # Never log request headers, response bodies, or transport exceptions.
                logger.warning("Airtable HTTP %d; retrying in %.1f seconds.", status, delay)
                time.sleep(delay)
                continue
            if status != 200:
                raise AirtableError(f"Airtable {path} failed with HTTP {status}.")
            try:
                payload = response.json()
            except ValueError:
                raise AirtableError(f"Airtable {path} returned invalid JSON.") from None
            if not isinstance(payload, dict):
                raise AirtableError(f"Airtable {path} returned a malformed response.")
            return payload
        raise AssertionError("HTTP retry budget exhausted")

    def pages(self, path: str, key: str, **params: Any) -> list[dict]:
        items: list[dict] = []
        offsets: set[str] = set()
        while True:
            payload = self.get(path, pageSize=100, **params)
            page = payload.get(key)
            if not isinstance(page, list) or any(not isinstance(item, dict) for item in page):
                raise AirtableError(f"Airtable {path} returned malformed {key}.")
            if key == "comments" and "offset" not in payload:
                raise AirtableError(f"Airtable {path} omitted its comments pagination offset.")
            items.extend(page)
            offset = payload.get("offset")
            if offset is None:
                return items
            if not isinstance(offset, str) or not offset or offset in offsets:
                raise AirtableError(f"Airtable {path} returned an invalid pagination offset.")
            offsets.add(offset)
            params["offset"] = offset


def _schema(client: _Client, base_id: str) -> dict[str, dict]:
    payload = client.get(f"meta/bases/{base_id}/tables")
    tables = payload.get("tables")
    if not isinstance(tables, list):
        raise AirtableError("Airtable schema response is missing its tables inventory.")
    result = {}
    for table in tables:
        if not isinstance(table, dict):
            raise AirtableError("Airtable returned a malformed table schema.")
        table_id = _identifier(table.get("id"), "table")
        if table_id in result or not isinstance(table.get("name"), str):
            raise AirtableError("Airtable returned duplicate or malformed table schemas.")
        fields = table.get("fields")
        if not isinstance(fields, list):
            raise AirtableError(f"Airtable table {table_id} is missing its fields schema.")
        field_ids = set()
        for field in fields:
            if not isinstance(field, dict):
                raise AirtableError(f"Airtable table {table_id} returned a malformed field.")
            field_id = _identifier(field.get("id"), "field")
            if (
                field_id in field_ids
                or not isinstance(field.get("name"), str)
                or not isinstance(field.get("type"), str)
            ):
                raise AirtableError(f"Airtable table {table_id} has duplicate or malformed fields.")
            field_ids.add(field_id)
        if table.get("primaryFieldId") not in field_ids:
            raise AirtableError(f"Airtable table {table_id} has an invalid primary field.")
        result[table_id] = table
    return result


def _modified_field(table: dict, configured: str | dict[str, str]) -> str:
    selected = configured.get(table["id"]) if isinstance(configured, dict) else configured
    matches = [f for f in table["fields"] if f["id"] == selected or f["name"] == selected]
    if len(matches) != 1:
        raise ValueError(f"Configure a Last modified time field for table {table['id']}.")
    field = matches[0]
    options = field.get("options") or {}
    result = options.get("result") or {}
    if (
        field["type"] != "lastModifiedTime"
        or options.get("isValid") is not True
        or result.get("type") != "dateTime"
    ):
        raise ValueError(
            f"Table {table['id']} needs a valid Last modified time field with time included."
        )
    return field["id"]


def _timestamp(value: Any) -> str | None:
    # Airtable omits blank fields; untouched/old records legitimately have no time.
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise AirtableError("Airtable returned a malformed Last modified time value.")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("timezone missing")
    except ValueError:
        raise AirtableError("Airtable returned a malformed Last modified time value.") from None
    return timestamp.astimezone(UTC).isoformat(timespec="microseconds")


def _attachments(value: Any) -> Any:
    """Keep stable attachment metadata, including on comment attachments."""
    if isinstance(value, list):
        return [_attachments(item) for item in value]
    if isinstance(value, dict):
        if isinstance(value.get("id"), str) and value["id"].startswith("att"):
            return {key: value[key] for key in _ATTACHMENT_KEYS if key in value}
        return {key: _attachments(item) for key, item in value.items()}
    return value


def _row(base_id: str, table_id: str, kind: str, item_id: str | None, title: str, text: str):
    document_id = f"{base_id}/{table_id}/{kind}"
    if item_id:
        document_id += f"/{item_id}"
    url = f"https://airtable.com/{base_id}/{table_id}"
    if item_id:
        url += f"/{item_id}"
    return {"id": document_id, "title": title, "content": text, "url": url, "_deleted": False}


def _schema_row(base_id: str, table: dict) -> dict:
    # A fixed whitelist also prevents views/volatile schema transport metadata churn.
    fields = [
        {
            key: field[key]
            for key in ("id", "name", "type", "description", "options")
            if key in field
        }
        for field in sorted(table["fields"], key=lambda item: item["id"])
    ]
    text = {
        "base_id": base_id,
        "table_id": table["id"],
        "name": table["name"],
        "description": table.get("description", ""),
        "primary_field_id": table["primaryFieldId"],
        "fields": fields,
    }
    return _row(base_id, table["id"], "schema", None, f"{table['name']} schema", _json(text))


def _comments(client: _Client, base_id: str, table_id: str, record_id: str) -> list[dict]:
    comments = client.pages(f"{base_id}/{table_id}/{record_id}/comments", "comments")
    result = []
    ids = set()
    for comment in comments:
        comment_id = _identifier(comment.get("id"), "comment")
        if comment_id in ids or not isinstance(comment.get("text"), str):
            raise AirtableError("Airtable returned duplicate or malformed comments.")
        ids.add(comment_id)
        author = comment.get("author")
        if not isinstance(author, dict) or not isinstance(comment.get("createdTime"), str):
            raise AirtableError("Airtable returned malformed comment metadata.")
        result.append(
            {
                "id": comment_id,
                "text": comment["text"],
                "created_time": _timestamp(comment["createdTime"]),
                "author": {key: author[key] for key in ("id", "name") if key in author},
                **{
                    key: _attachments(comment[key])
                    for key in ("parentCommentId", "attachments", "mentioned")
                    if key in comment
                },
            }
        )
    return sorted(result, key=lambda comment: (comment["created_time"] or "", comment["id"]))


def _record_row(
    client: _Client, base_id: str, table: dict, record: dict, modified: str, include_comments: bool
) -> dict:
    record_id = _identifier(record.get("id"), "record")
    values = record.get("fields")
    if not isinstance(values, dict):
        raise AirtableError("Airtable returned a record without its fields object.")
    field_map = {field["id"]: field for field in table["fields"]}
    if set(values) - set(field_map):
        raise AirtableError("Airtable returned fields absent from the current schema; retry sync.")
    fields = [
        {
            "id": field_id,
            "name": field_map[field_id]["name"],
            "value": _attachments(values[field_id]),
        }
        for field_id in sorted(values)
        if field_id != modified
    ]
    text = {
        "base_id": base_id,
        "table_id": table["id"],
        "record_id": record_id,
        "table": table["name"],
        "fields": fields,
    }
    if include_comments:
        text["comments"] = _comments(client, base_id, table["id"], record_id)
    primary = values.get(table["primaryFieldId"])
    title = primary if isinstance(primary, str) and primary.strip() else record_id
    return _row(base_id, table["id"], "record", record_id, f"{table['name']}: {title}", _json(text))


def _reconcile(
    session: Any,
    base_id: str,
    state: dict,
    *,
    table_ids: list[str] | None = None,
    last_modified_field: str | dict[str, str] = "Last modified time",
    include_schema: bool = True,
    include_comments: bool = True,
    token: str | None = None,
) -> tuple[list[dict], dict]:
    """Return a complete delta and candidate state without mutating ``state``."""
    client = _Client(session, token)
    schema = _schema(client, base_id)
    next_state = copy.deepcopy(state)
    previous = state.get("tables", {})
    selected = set(table_ids) if table_ids is not None else set(schema) | set(previous)
    unknown = selected - set(schema) - set(previous)
    if unknown:
        raise ValueError(f"Unknown Airtable table ID(s): {', '.join(sorted(unknown))}.")
    next_tables = next_state.setdefault("tables", {})
    rows = []
    for table_id in sorted(selected):
        old = previous.get(table_id, {})
        known = old.get("documents", {})
        if table_id not in schema:
            rows.extend({"id": doc_id, "_deleted": True} for doc_id in sorted(known))
            # Remember a confirmed disappearance so explicit selection remains valid
            # and a subsequently restored table can be backfilled.
            next_tables[table_id] = {"watermark": old.get("watermark"), "documents": {}}
            continue
        table = schema[table_id]
        modified = _modified_field(table, last_modified_field)
        records = client.pages(f"{base_id}/{table_id}", "records", returnFieldsByFieldId="true")
        current = {}
        seen_records = set()
        watermark = old.get("watermark")
        documents = [_schema_row(base_id, table)] if include_schema else []
        for record in records:
            record_id = _identifier(record.get("id"), "record")
            if record_id in seen_records:
                raise AirtableError("Airtable returned duplicate records; inventory is incomplete.")
            seen_records.add(record_id)
            row = _record_row(client, base_id, table, record, modified, include_comments)
            timestamp = _timestamp(record["fields"].get(modified))
            if timestamp and (not watermark or timestamp > watermark):
                watermark = timestamp
            documents.append(row)
        for row in documents:
            digest = _fingerprint(row)
            current[row["id"]] = digest
            if digest != known.get(row["id"]):
                rows.append(row)
        rows.extend(
            {"id": doc_id, "_deleted": True} for doc_id in sorted(set(known) - set(current))
        )
        next_tables[table_id] = {"watermark": watermark, "documents": current}
    return rows, next_state


def airtable_source(
    *,
    base_id: str,
    table_ids: list[str] | None = None,
    token: str | None = None,
    last_modified_field: str | dict[str, str] = "Last modified time",
    include_schema: bool = True,
    include_comments: bool = True,
    session: Any = None,
) -> DltResource:
    """Create an incremental document resource for a selected Airtable base.

    Pass the returned resource to ``cognee.remember`` with
    ``dlt_config={"primary_key": "id", "write_disposition": "merge",
    "max_rows_per_table": 0}``. Metadata permission is required even when
    ``include_schema=False``. ``token`` falls back to ``AIRTABLE_ACCESS_TOKEN``
    and authenticates every request, including injected transports. Injected
    sessions remain caller-owned and are never closed by the connector.
    """
    import dlt

    _identifier(base_id, "base")
    if table_ids is not None:
        if not isinstance(table_ids, list) or any(not isinstance(item, str) for item in table_ids):
            raise ValueError("table_ids must be a list of Airtable table IDs or None.")
        for table_id in table_ids:
            _identifier(table_id, "table")
        table_ids = list(dict.fromkeys(table_ids))
    if not isinstance(last_modified_field, (str, dict)) or not last_modified_field:
        raise ValueError("last_modified_field must be a field name/ID or a table-ID mapping.")
    if isinstance(last_modified_field, dict) and any(
        not isinstance(key, str) or not isinstance(value, str) or not value
        for key, value in last_modified_field.items()
    ):
        raise ValueError("last_modified_field mapping must contain table IDs and field names/IDs.")
    resolved_token = token or os.environ.get("AIRTABLE_ACCESS_TOKEN")
    if not isinstance(resolved_token, str) or not resolved_token.strip():
        raise ValueError("Pass token= or set AIRTABLE_ACCESS_TOKEN to an Airtable PAT.")
    resource_name = "airtable_documents_" + hashlib.sha256(base_id.encode()).hexdigest()[:16]

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def documents():
        client_session = session
        if client_session is None:
            client_session = requests.Session()
        try:
            state = dlt.current.resource_state()
            rows, candidate_state = _reconcile(
                client_session,
                base_id,
                state,
                table_ids=table_ids,
                last_modified_field=last_modified_field,
                include_schema=include_schema,
                include_comments=include_comments,
                token=resolved_token,
            )
            yield from rows
            state.update(candidate_state)
            logger.info(
                "Airtable: reconciled base %s, emitting %d document changes.", base_id, len(rows)
            )
        finally:
            if session is None:
                client_session.close()

    resource = documents()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "airtable")
    setattr(resource, PIPELINE_SCOPE_ATTR, f"airtable:{base_id}")
    return resource
