"""DLT source for syncing Fibery entities as markdown documents.

Entity listings are swept with Fibery's stable ``fibery/id`` keyset cursor.
The listing supplies the complete id snapshot and modification dates; document
content is fetched only for changed or newly seen entities, in batches. This
keeps the cost at O(N) lightweight entity rows plus O(C / B) document requests
for N entities, C changed entities, and Fibery's bounded document batch size B.

The source stores an id set and modification cursor separately for each
selected database. Deleted ids are emitted only after the database query,
document requests, and row conversions all complete successfully. Any API or
parsing exception escapes the DLT resource, so a partial read can never be
mistaken for a deletion sweep. Fibery has no deletion feed, which makes this
completed-snapshot check the safety boundary for hard-delete tombstones.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("fibery_connector")

FIBERY_TABLE_NAME = "fibery_entities"
FIBERY_SOURCE_NAME = "fibery"
_PAGE_SIZE = 250
_DOCUMENT_BATCH_SIZE = 100
_MAX_RETRIES = 5
_MAX_RETRY_DELAY = 10.0
_EXTRA_HINT = 'Install the Fibery connector extra: pip install "cognee-community-connector-fibery".'


def fibery_source(
    account: str,
    databases: list[str],
    token: str | None = None,
    client: Any = None,
):
    """Create a dlt source that syncs selected Fibery databases as documents.

    Args:
        account: Fibery workspace subdomain (the ``account`` in
            ``https://<account>.fibery.io``).
        databases: Fibery type names, for example ``["Product/Feature"]``.
        token: API token. Falls back to ``FIBERY_API_TOKEN``.
        client: Optional requests-compatible session used for testing.
    """
    account = account.strip().lower()
    if not account or "/" in account or "." in account:
        raise ValueError("account must be a Fibery workspace subdomain.")
    databases = list(dict.fromkeys(db.strip() for db in databases if db.strip()))
    if not databases:
        raise ValueError("At least one Fibery database is required.")

    if client is None:
        resolved_token = token or os.environ.get("FIBERY_API_TOKEN")
        if not resolved_token:
            raise ValueError("Fibery API token required: pass token= or set FIBERY_API_TOKEN.")
        client = _make_client(resolved_token)

    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    base_url = f"https://{account}.fibery.io"

    @dlt.resource(
        name=FIBERY_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def fibery_entities():
        state = dlt.current.resource_state()
        yield from sync_entities(client, base_url, databases, state)

    @dlt.source(name=FIBERY_SOURCE_NAME)
    def _fibery():
        return fibery_entities

    source = _fibery()
    setattr(source, DOCUMENT_SOURCE_ATTR, FIBERY_SOURCE_NAME)
    return source


def _make_client(token: str) -> Any:
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - optional package dependency
        raise ImportError(_EXTRA_HINT) from exc
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Token {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
    )
    return session


def sync_entities(
    client: Any,
    base_url: str,
    databases: list[str],
    state: dict,
) -> Iterator[dict[str, Any]]:
    """Yield changed entity documents and tombstones for completed snapshots."""
    # Deletion correctness depends on separating the cheap inventory pass from
    # content retrieval. First walk every selected type using Fibery's indexed
    # ``fibery/id`` cursor, recording the complete current id set and the
    # modification date carried by each entity. Only after this pass succeeds
    # do we compare it with the last committed id set and fetch rich-text bodies
    # for the delta. That means a failure on any page or document request exits
    # before a tombstone can be produced and before this database's state moves
    # forward; an incomplete network response therefore cannot erase memory.
    #
    # The sweep necessarily costs O(N) because Fibery has no deletion feed and
    # every id must be checked to discover removed entities. Keyset pagination
    # keeps that O(N) scan linear instead of repeatedly skipping growing offset
    # prefixes, while batching the documents reduces content calls to O(C / B)
    # for C changed entities and batch size B. In particular, unchanged rows
    # participate only in the id/date sweep and incur no document request.
    schema = _schema(client, base_url)
    types = {
        item.get("fibery/name"): item
        for item in schema.get("fibery/types", [])
        if isinstance(item, dict) and item.get("fibery/name")
    }
    database_state = state.setdefault("databases", {})

    for database in databases:
        type_schema = types.get(database)
        if type_schema is None:
            raise ValueError(f"Fibery database {database!r} was not found in the workspace schema.")

        prior = database_state.get(database, {})
        known_ids = set(prior.get("known_ids", []))
        last_modified = prior.get("last_modified", "")
        selectors = _selectors(type_schema)
        records = list(_query_entities(client, base_url, database, selectors))
        current_ids = {str(row["fibery/id"]) for row in records if row.get("fibery/id")}

        changed = [
            row
            for row in records
            if row.get("fibery/id")
            and (str(row["fibery/id"]) not in known_ids
                 or str(row.get("fibery/modification-date") or "") > last_modified)
        ]
        newest_modified = max(
            [last_modified]
            + [str(row.get("fibery/modification-date") or "") for row in records]
        )

        # Resolve all collaborative documents in bounded batches before writing
        # tombstones. A failed batch aborts this resource run, preserving state.
        documents = _documents_for_rows(client, base_url, type_schema, changed)
        for row in changed:
            yield _entity_to_row(
                account_url=base_url,
                database=database,
                row=row,
                documents=documents,
            )

        # All selected databases are independently scoped in state so changing
        # the configured set cannot accidentally compare ids across types.
        for entity_id in sorted(known_ids - current_ids):
            yield {"id": f"{database}:{entity_id}", "_deleted": True}

        database_state[database] = {
            "known_ids": sorted(current_ids),
            "last_modified": newest_modified,
        }
        logger.info(
            "Fibery %s: synced %d changed entity(s), %d deletion(s).",
            database,
            len(changed),
            len(known_ids - current_ids),
        )


def _schema(client: Any, base_url: str) -> dict:
    result = _post_command(client, base_url, {"command": "fibery.schema/query"})
    return result or {}


def _selectors(type_schema: dict) -> list[Any]:
    """Select identity, title, timestamps, scalar fields, refs, and doc secrets."""
    fields = [f for f in type_schema.get("fibery/fields", []) if isinstance(f, dict)]
    title = next(
        (
            field.get("fibery/name")
            for field in fields
            if field.get("fibery/name")
            and (
                field.get("fibery/name", "").endswith("/Name")
                or (field.get("fibery/meta") or {}).get("fibery/name?")
            )
        ),
        None,
    )
    selected: list[Any] = ["fibery/id", "fibery/public-id", "fibery/modification-date"]
    if title and title not in selected:
        selected.append(title)
    for field in fields:
        name, field_type = field.get("fibery/name"), field.get("fibery/type", "")
        if not name or name in selected or name in {
            "fibery/creation-date",
            "fibery/created-by",
            "fibery/modified-by",
        }:
            continue
        if field_type in {"Collaboration~Documents/Document", "collaboration~documents/document"}:
            selected.append({name: ["Collaboration~Documents/secret"]})
        elif field_type.startswith("fibery/") and not (field.get("fibery/meta") or {}).get(
            "fibery/collection?"
        ):
            selected.append(name)
        elif (field.get("fibery/meta") or {}).get("fibery/collection?") and "comments" in name.lower():
            selected.append({name: {"q/select": _comment_selectors(), "q/limit": 100}})
        elif field_type and not field_type.startswith("fibery/"):
            selected.append({name: ["fibery/id", "fibery/public-id"]})
    return selected


def _comment_selectors() -> list[Any]:
    return [
        "fibery/id",
        "fibery/creation-date",
        "comment/document-secret",
        {"comment/author": ["fibery/id"]},
    ]


def _query_entities(client: Any, base_url: str, database: str, selectors: list[Any]):
    """Keyset paginate on the stable id; avoids offset scans on large tables."""
    cursor = None
    while True:
        query: dict[str, Any] = {
            "q/from": database,
            "q/select": selectors,
            "q/order-by": [[ ["fibery/id"], "q/asc" ]],
            "q/limit": _PAGE_SIZE + 1,
        }
        params = None
        if cursor:
            query["q/where"] = [">", ["fibery/id"], "$last-seen-id"]
            params = {"$last-seen-id": cursor}
        payload = {"command": "fibery.entity/query", "args": {"query": query}}
        if params:
            payload["args"]["params"] = params
        response = _post_command(client, base_url, payload)
        rows = response if isinstance(response, list) else []
        page = rows[:_PAGE_SIZE]
        yield from page
        if len(rows) <= _PAGE_SIZE:
            return
        next_cursor = page[-1].get("fibery/id") if page else None
        if not next_cursor or next_cursor == cursor:
            raise RuntimeError(f"Fibery pagination did not advance for database {database!r}.")
        cursor = next_cursor


def _documents_for_rows(
    client: Any,
    base_url: str,
    type_schema: dict,
    rows: list[dict],
) -> dict[str, str]:
    doc_field_names = {
        field.get("fibery/name")
        for field in type_schema.get("fibery/fields", [])
        if field.get("fibery/type", "").lower() == "collaboration~documents/document"
    }
    secrets: list[str] = []
    for row in rows:
        for name in doc_field_names:
            value = row.get(name)
            if isinstance(value, dict):
                secret = value.get("Collaboration~Documents/secret")
                if secret:
                    secrets.append(str(secret))
        for comment in _comments(row):
            if comment.get("comment/document-secret"):
                secrets.append(str(comment["comment/document-secret"]))

    content_by_secret: dict[str, str] = {}
    # The batch endpoint keeps API calls proportional to batches rather than
    # documents and therefore respects Fibery's per-token request quota better.
    unique_secrets = list(dict.fromkeys(secrets))
    for start in range(0, len(unique_secrets), _DOCUMENT_BATCH_SIZE):
        batch = unique_secrets[start : start + _DOCUMENT_BATCH_SIZE]
        result = _request(
            client,
            "POST",
            f"{base_url}/api/documents/commands?format=md",
            json={"command": "get-documents", "args": [{"secret": secret} for secret in batch]},
        )
        for document in result or []:
            if isinstance(document, dict) and document.get("secret"):
                content_by_secret[str(document["secret"])] = str(document.get("content") or "")
    return content_by_secret


def _comments(row: dict) -> list[dict]:
    for name, value in row.items():
        if "comments" in name.lower() and isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _entity_to_row(account_url: str, database: str, row: dict, documents: dict[str, str]) -> dict:
    entity_id = str(row.get("fibery/id") or "")
    public_id = row.get("fibery/public-id")
    title = ""
    body: list[str] = []
    for name, value in row.items():
        if name.endswith("/Name") and isinstance(value, str) and not title:
            title = value
        if isinstance(value, dict):
            secret = value.get("Collaboration~Documents/secret")
            if secret and documents.get(str(secret)):
                body.append(documents[str(secret)])
            elif value and not name.startswith("fibery/"):
                body.append(f"{name.rsplit('/', 1)[-1]}: {_format_relation(value)}")
        elif isinstance(value, list) and value:
            body.append(f"{name.rsplit('/', 1)[-1]}: {_format_relation(value)}")
        elif isinstance(value, (str, int, float, bool)) and not name.startswith("fibery/"):
            body.append(f"{name.rsplit('/', 1)[-1]}: {value}")

    for comment in _comments(row):
        secret = comment.get("comment/document-secret")
        text = documents.get(str(secret), "") if secret else ""
        if text:
            body.append(f"Comment:\n{text}")

    url = f"{account_url}/?entityId={quote(entity_id)}"
    if public_id:
        url += f"&publicId={quote(str(public_id))}"
    return {
        "id": f"{database}:{entity_id}",
        "url": url,
        "title": title or f"{database} {public_id or entity_id}",
        "content": "\n\n".join(body),
        "_deleted": False,
    }


def _format_relation(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(_format_relation(item) for item in value)
    if isinstance(value, dict):
        return str(
            value.get("fibery/public-id")
            or value.get("fibery/id")
            or value.get("fibery/name")
            or ""
        )
    return str(value)


def _post_command(client: Any, base_url: str, payload: dict) -> Any:
    response = _request(client, "POST", f"{base_url}/api/commands", json=payload)
    if isinstance(response, dict) and response.get("success") is False:
        raise RuntimeError(f"Fibery command failed: {response.get('error') or response}")
    return response.get("result") if isinstance(response, dict) else response


def _request(client: Any, method: str, url: str, **kwargs) -> Any:
    for attempt in range(_MAX_RETRIES):
        try:
            response = getattr(client, method.lower())(url, **kwargs)
            status_code = getattr(response, "status_code", 200)
            if status_code == 429 or status_code >= 500:
                if attempt == _MAX_RETRIES - 1:
                    response.raise_for_status()
                delay = _retry_delay(getattr(response, "headers", {}), attempt)
                logger.warning(
                    "Fibery rate/transient response; retrying in %.1fs (%d/%d).",
                    delay,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(delay)
                continue
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            status = getattr(exc, "response", None)
            status_code = getattr(status, "status_code", None)
            if attempt == _MAX_RETRIES - 1 or (
                status_code and status_code not in (429, 500, 502, 503, 504)
            ):
                raise
            if status_code is None and not _is_network_error(exc):
                raise
            delay = _retry_delay(getattr(status, "headers", {}), attempt)
            logger.warning(
                "Fibery request failed; retrying in %.1fs (%d/%d): %s",
                delay,
                attempt + 1,
                _MAX_RETRIES,
                exc,
            )
            time.sleep(delay)
    raise RuntimeError("Fibery request retry loop exhausted.")


def _retry_delay(headers: Any, attempt: int) -> float:
    raw = (headers or {}).get("Retry-After") or (headers or {}).get("retry-after")
    try:
        delay = float(raw)
    except (TypeError, ValueError):
        delay = float(2**attempt)
    return min(max(delay, 0.0), _MAX_RETRY_DELAY)


def _is_network_error(exc: Exception) -> bool:
    try:
        import requests
        return isinstance(exc, requests.RequestException)
    except ImportError:
        return False
