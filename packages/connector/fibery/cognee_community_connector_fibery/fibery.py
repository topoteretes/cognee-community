"""DLT source that syncs selected Fibery databases as markdown documents."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import quote

logger = logging.getLogger(__name__)

FIBERY_TABLE_NAME = "fibery_entities"
FIBERY_SOURCE_NAME = "fibery"
_PAGE_SIZE = 100
_DOCUMENT_BATCH = 50
_COLLECTION_LIMIT = 1000
_MAX_RETRIES = 4
_MAX_SLEEP = 8.0
_PRIMITIVES = frozenset(
    {
        "fibery/text",
        "fibery/int",
        "fibery/decimal",
        "fibery/bool",
        "fibery/date",
        "fibery/date-time",
        "fibery/uuid",
        "fibery/rank",
        "fibery/emoji",
    }
)
_SKIP = frozenset(
    {
        "fibery/id",
        "fibery/public-id",
        "fibery/creation-date",
        "fibery/modification-date",
        "fibery/created-by",
        "fibery/modified-by",
    }
)
_DOCUMENT_TYPE = "collaboration~documents/document"


class FiberyError(RuntimeError):
    """Fibery could not be read. The message never includes the token or a document body."""


class FiberyAPIError(FiberyError):
    """Fibery returned an error, or the response was not usable."""


class FiberyRateLimitError(FiberyAPIError):
    """A 429 asked us to wait longer than one sync may hold the staging lock."""


def fibery_source(
    account: str,
    databases: list[str],
    token: str | None = None,
    client: Any = None,
):
    """Return a dlt source of one markdown document per Fibery entity.

    Pass the result to ``cognee.remember`` with ``primary_key="id"``,
    ``write_disposition="merge"`` and ``max_rows_per_table=0``. ``account`` is
    the workspace subdomain. ``databases`` are ``Space/Database`` names.
    ``token`` falls back to ``FIBERY_API_TOKEN``. ``client`` is the test double
    for ``command`` and ``documents``.
    """
    account_name = _account_name(account)
    selected = _database_names(databases)
    base_url = f"https://{account_name}.fibery.io"
    resolved = None
    if client is None:
        resolved = str(token or os.environ.get("FIBERY_API_TOKEN") or "").strip()
        if not resolved:
            raise ValueError("Fibery API token required: pass token= or set FIBERY_API_TOKEN.")

    from cognee.tasks.ingestion.dlt_utils import (
        DOCUMENT_SOURCE_ATTR,
        DOCUMENT_SYNC_VERSION,
        PIPELINE_SCOPE_ATTR,
    )

    if DOCUMENT_SYNC_VERSION < 1:
        raise RuntimeError("Fibery sync requires Cognee table-scoped document sync support.")

    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'Install the Fibery connector: pip install "cognee-community-connector-fibery".'
        ) from exc

    if client is None:
        client = FiberyClient(resolved, base_url)
    owned = resolved is not None

    @dlt.resource(
        name=FIBERY_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def fibery_entities():
        try:
            if dlt.current.resource().write_disposition != "merge":
                raise ValueError("Fibery requires write_disposition='merge'.")
            produced = False
            for row in sync_entities(client, base_url, selected, dlt.current.resource_state()):
                produced = True
                yield row
            if not produced:
                yield dlt.mark.materialize_table_schema()
        finally:
            if owned:
                client.close()

    @dlt.source(name=FIBERY_SOURCE_NAME)
    def _fibery():
        return fibery_entities

    source = _fibery()
    setattr(source, DOCUMENT_SOURCE_ATTR, FIBERY_SOURCE_NAME)
    setattr(source, PIPELINE_SCOPE_ATTR, f"fibery:{account_name}")
    return source


class FiberyClient:
    """HTTP client for the Fibery commands and document APIs."""

    def __init__(self, token: str, base_url: str, http: Any = None):
        self._headers = {
            "Authorization": f"Token {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        self._base = base_url
        self._http = http
        self._owned = http is None

    def close(self) -> None:
        if self._owned and self._http is not None:
            self._http.close()

    def command(self, command: str, args: dict | None = None) -> Any:
        payload: dict[str, Any] = {"command": command}
        if args:
            payload["args"] = args
        response = _request(
            lambda: self._session().post(
                f"{self._base}/api/commands",
                json=payload,
                headers=self._headers,
            )
        )
        body = _json_object(response)
        if body.get("success") is not True:
            raise FiberyAPIError("Fibery command failed.")
        return body.get("result")

    def documents(self, secrets: list[str]) -> dict[str, str]:
        found: dict[str, str] = {}
        for start in range(0, len(secrets), _DOCUMENT_BATCH):
            batch = secrets[start : start + _DOCUMENT_BATCH]
            payload = {
                "command": "get-documents",
                "args": [{"secret": secret} for secret in batch],
            }
            response = _request(
                lambda batch_payload=payload: self._session().post(
                    f"{self._base}/api/documents/commands?format=md",
                    json=batch_payload,
                    headers=self._headers,
                )
            )
            try:
                body = response.json()
            except Exception as exc:
                raise FiberyAPIError("Fibery document response was not JSON.") from exc
            rows = body.get("result") if isinstance(body, dict) else body
            if not isinstance(rows, list):
                raise FiberyAPIError("Fibery document batch was not a list.")
            got: dict[str, str] = {}
            for item in rows:
                if isinstance(item, dict) and isinstance(item.get("secret"), str):
                    got[item["secret"]] = str(item.get("content") or "")
            if not set(batch).issubset(got):
                raise FiberyAPIError("Fibery document batch omitted a requested secret.")
            found.update(got)
        return found

    def _session(self):
        if self._http is None:
            import httpx

            self._http = httpx.Client(headers=self._headers, timeout=30.0)
        return self._http


def sync_entities(
    client: Any,
    base_url: str,
    databases: list[str],
    state: dict,
    *,
    page_size: int = _PAGE_SIZE,
) -> Iterator[dict[str, Any]]:
    """Yield changed entity documents, then tombstones, after every database is read."""
    types = _types(client.command("fibery.schema/query"))
    prepared: list[dict[str, Any]] = []
    stored = state.get("databases") or {}
    for database in databases:
        type_schema = types.get(database)
        if type_schema is None:
            raise FiberyAPIError(f"Fibery database {database!r} was not found.")
        selectors, doc_fields, title_field = _selectors(type_schema, types)
        records = list(_query_entities(client, database, selectors, page_size))
        prior = stored.get(database) or {}
        known = {str(entity_id) for entity_id in prior.get("known_ids") or []}
        last_modified = str(prior.get("last_modified") or "")
        changed: list[dict] = []
        current_ids: list[str] = []
        newest = last_modified
        for row in records:
            entity_id = str(row["fibery/id"])
            current_ids.append(entity_id)
            modified = str(row.get("fibery/modification-date") or "")
            if modified > newest:
                newest = modified
            if entity_id not in known or (modified and modified > last_modified):
                changed.append(row)
        secrets = _secrets(changed, doc_fields)
        documents = client.documents(secrets) if secrets else {}
        prepared.append(
            {
                "database": database,
                "changed": changed,
                "known": known,
                "current_ids": current_ids,
                "newest": newest,
                "documents": documents,
                "doc_fields": doc_fields,
                "title_field": title_field,
            }
        )

    updates: dict[str, dict] = {}
    for item in prepared:
        database = item["database"]
        for row in item["changed"]:
            yield _entity_row(
                base_url,
                database,
                row,
                item["documents"],
                item["doc_fields"],
                item["title_field"],
            )
        removed = item["known"] - set(item["current_ids"])
        for entity_id in sorted(removed):
            yield {"id": f"{database}:{entity_id}", "_deleted": True}
        updates[database] = {
            "known_ids": sorted(item["current_ids"]),
            "last_modified": item["newest"],
        }
        logger.info(
            "Fibery %s: synced %d changed entity(s), %d deletion(s).",
            database,
            len(item["changed"]),
            len(removed),
        )
    state["databases"] = {**stored, **updates}


def _request(call: Callable[[], Any]) -> Any:
    try:
        import httpx
    except ImportError:
        transport_errors: tuple[type[BaseException], ...] = ()
    else:
        transport_errors = (httpx.TransportError,)

    for attempt in range(_MAX_RETRIES):
        try:
            response = call()
        except transport_errors:
            if attempt == _MAX_RETRIES - 1:
                raise FiberyAPIError("Fibery request failed.") from None
            time.sleep(_delay(None, attempt))
            continue
        status = getattr(response, "status_code", 200)
        if status == 429 or status >= 500:
            if attempt == _MAX_RETRIES - 1:
                raise FiberyAPIError(f"Fibery request failed with status {status}.")
            time.sleep(_delay(getattr(response, "headers", None), attempt))
            continue
        if status >= 400:
            raise FiberyAPIError(f"Fibery request failed with status {status}.")
        return response
    raise FiberyAPIError("Fibery request failed.")


def _delay(headers: Any, attempt: int) -> float:
    raw = None if not headers else headers.get("Retry-After", headers.get("retry-after"))
    try:
        delay = float(raw)
    except (TypeError, ValueError):
        delay = float(2**attempt)
    if delay < 0 or delay > _MAX_SLEEP:
        raise FiberyRateLimitError("Fibery retry wait exceeds the sync budget.")
    return delay


def _json_object(response: Any) -> dict:
    try:
        body = response.json()
    except Exception as exc:
        raise FiberyAPIError("Fibery response was not JSON.") from exc
    if not isinstance(body, dict):
        raise FiberyAPIError("Fibery response was not an object.")
    return body


def _account_name(account: str) -> str:
    name = account.strip().lower()
    if not name or name.startswith("-") or not name.replace("-", "").isalnum():
        raise ValueError("account must be a Fibery workspace subdomain.")
    return name


def _database_names(databases: list[str]) -> list[str]:
    selected: list[str] = []
    seen: set[str] = set()
    for item in databases:
        name = item.strip()
        if not name or name in seen:
            continue
        space, separator, database = name.partition("/")
        if not separator or not space or not database or "/" in database:
            raise ValueError(f"Fibery database must be Space/Name, got {name!r}.")
        seen.add(name)
        selected.append(name)
    if not selected:
        raise ValueError("At least one Fibery database is required.")
    return selected


def _types(schema: Any) -> dict[str, dict]:
    if not isinstance(schema, dict) or not isinstance(schema.get("fibery/types"), list):
        raise FiberyAPIError("Fibery schema has no types.")
    return {
        item["fibery/name"]: item
        for item in schema["fibery/types"]
        if isinstance(item, dict) and isinstance(item.get("fibery/name"), str)
    }


def _fields(type_schema: dict) -> list[dict]:
    return [
        field
        for field in type_schema.get("fibery/fields") or []
        if isinstance(field, dict) and isinstance(field.get("fibery/name"), str)
    ]


def _title_field(type_schema: dict) -> str | None:
    fields = _fields(type_schema)
    for field in fields:
        meta = field.get("fibery/meta") or {}
        if meta.get("fibery/name?") or meta.get("ui/title?"):
            return field["fibery/name"]
    for field in fields:
        if field["fibery/name"].endswith("/Name") and field.get("fibery/type") in _PRIMITIVES:
            return field["fibery/name"]
    for field in fields:
        if field["fibery/name"] == "enum/name":
            return "enum/name"
    return None


def _is_collection(field: dict) -> bool:
    return bool((field.get("fibery/meta") or {}).get("fibery/collection?"))


def _is_document(field: dict) -> bool:
    return str(field.get("fibery/type") or "").lower() == _DOCUMENT_TYPE


def _is_comment(field: dict) -> bool:
    return (
        field.get("fibery/type") == "comments/comment"
        or field["fibery/name"] == "comments/comments"
    )


def _label_fields(types: dict[str, dict], type_name: str) -> list[str]:
    selected = ["fibery/id", "fibery/public-id"]
    target = types.get(type_name)
    label = _title_field(target) if target is not None else None
    if label and label not in selected:
        selected.append(label)
    return selected


def _selectors(
    type_schema: dict, types: dict[str, dict]
) -> tuple[list[Any], list[str], str | None]:
    selected: list[Any] = ["fibery/id", "fibery/public-id", "fibery/modification-date"]
    doc_fields: list[str] = []
    for field in _fields(type_schema):
        name = field["fibery/name"]
        if name in _SKIP:
            continue
        if _is_document(field):
            selected.append({name: ["Collaboration~Documents/secret"]})
            doc_fields.append(name)
        elif field.get("fibery/type") in _PRIMITIVES and not _is_collection(field):
            selected.append(name)
        elif _is_collection(field) and _is_comment(field):
            selected.append(
                {
                    name: {
                        "q/select": [
                            "fibery/id",
                            "comment/document-secret",
                            "fibery/creation-date",
                        ],
                        "q/limit": _COLLECTION_LIMIT,
                    }
                }
            )
        else:
            picked = _label_fields(types, str(field.get("fibery/type") or ""))
            if _is_collection(field):
                selected.append({name: {"q/select": picked, "q/limit": _COLLECTION_LIMIT}})
            else:
                selected.append({name: picked})
    return selected, doc_fields, _title_field(type_schema)


def _query_entities(client: Any, database: str, selectors: list[Any], page_size: int):
    cursor = None
    seen: set[str] = set()
    while True:
        query: dict[str, Any] = {
            "q/from": database,
            "q/select": selectors,
            "q/order-by": [[["fibery/id"], "q/asc"]],
            "q/limit": page_size + 1,
        }
        args: dict[str, Any] = {"query": query}
        if cursor is not None:
            query["q/where"] = [">", ["fibery/id"], "$last-seen-id"]
            args["params"] = {"$last-seen-id": cursor}
        result = client.command("fibery.entity/query", args)
        if not isinstance(result, list):
            raise FiberyAPIError(f"Fibery query for {database!r} did not return a list.")
        if len(result) > page_size + 1:
            raise FiberyAPIError(f"Fibery query for {database!r} exceeded the page size.")
        page = result[:page_size]
        for row in page:
            if not isinstance(row, dict) or not row.get("fibery/id"):
                raise FiberyAPIError(
                    f"Fibery query for {database!r} returned an entity without an id."
                )
            entity_id = str(row["fibery/id"])
            if entity_id in seen:
                raise FiberyAPIError(f"Fibery pagination for {database!r} did not advance.")
            seen.add(entity_id)
            yield row
        if len(result) <= page_size:
            return
        cursor = str(page[-1]["fibery/id"])


def _secrets(rows: list[dict], doc_fields: list[str]) -> list[str]:
    secrets: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for name in doc_fields:
            secret = _document_secret(row.get(name))
            if secret and secret not in seen:
                seen.add(secret)
                secrets.append(secret)
        for comment in _comment_rows(row):
            secret = comment.get("comment/document-secret")
            if isinstance(secret, str) and secret and secret not in seen:
                seen.add(secret)
                secrets.append(secret)
    return secrets


def _document_secret(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    secret = value.get("Collaboration~Documents/secret")
    if isinstance(secret, str) and secret:
        return secret
    return None


def _is_comment_item(item: Any) -> bool:
    return isinstance(item, dict) and isinstance(item.get("comment/document-secret"), str)


def _ordered_comments(items: list) -> list[dict]:
    comments = [item for item in items if _is_comment_item(item)]
    comments.sort(
        key=lambda item: (
            str(item.get("fibery/creation-date") or ""),
            str(item.get("fibery/id") or ""),
        )
    )
    return comments


def _comment_rows(row: dict) -> list[dict]:
    comments: list[dict] = []
    for value in row.values():
        if isinstance(value, list):
            comments.extend(_ordered_comments(value))
    return comments


def _entity_row(
    base_url: str,
    database: str,
    row: dict,
    documents: dict[str, str],
    doc_fields: list[str],
    title_field: str | None,
) -> dict[str, Any]:
    entity_id = str(row["fibery/id"])
    public_id = row.get("fibery/public-id")
    title = row.get(title_field) if title_field else ""
    if not isinstance(title, str):
        title = ""
    parts: list[str] = []
    doc_names = set(doc_fields)
    for name, value in row.items():
        if name in _SKIP or name == title_field:
            continue
        if name in doc_names:
            secret = _document_secret(value)
            text = documents.get(secret, "") if secret else ""
            if text:
                parts.append(text)
            continue
        rendered = _render_value(name, value, documents)
        if rendered:
            parts.append(rendered)
    return {
        "id": f"{database}:{entity_id}",
        "url": _entity_url(base_url, database, entity_id, public_id),
        "title": title or f"{database} {public_id or entity_id}",
        "content": "\n\n".join(parts),
        "_deleted": False,
    }


def _render_value(name: str, value: Any, documents: dict[str, str]) -> str:
    label = name.rsplit("/", 1)[-1]
    if isinstance(value, list):
        comments = _ordered_comments(value)
        if comments:
            chunks = [documents.get(item["comment/document-secret"], "") for item in comments]
            chunks = [chunk for chunk in chunks if chunk]
            if not chunks:
                return ""
            return "\n\n".join(f"Comment:\n{chunk}" for chunk in chunks)
        chunks = [_ref_label(item) for item in value]
        chunks = [chunk for chunk in chunks if chunk]
        if not chunks:
            return ""
        return f"{label}: {', '.join(chunks)}"
    if isinstance(value, dict):
        shown = _ref_label(value)
        return f"{label}: {shown}" if shown else ""
    if isinstance(value, (str, int, float, bool)) and value != "":
        return f"{label}: {value}"
    return ""


def _ref_label(value: Any) -> str:
    if not isinstance(value, dict):
        return "" if value in (None, "") else str(value)
    if isinstance(value.get("enum/name"), str) and value["enum/name"]:
        return value["enum/name"]
    for key, item in value.items():
        if key.endswith("/Name") and isinstance(item, str) and item:
            return item
    for key, item in value.items():
        if key.endswith("/name") and isinstance(item, str) and item:
            return item
    public_id = value.get("fibery/public-id")
    if public_id not in (None, ""):
        return str(public_id)
    return ""


def _entity_url(base_url: str, database: str, entity_id: str, public_id: Any) -> str:
    space, _, name = database.partition("/")
    if public_id not in (None, "") and space and name:
        return f"{base_url}/{quote(space)}/{quote(name)}/{quote(str(public_id))}"
    return f"{base_url}/?entityId={quote(entity_id)}"
