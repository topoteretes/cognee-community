"""Incremental Postman extraction with a complete, replayable document snapshot."""

import copy
import hashlib
import json
import os
import re
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from typing import TYPE_CHECKING, Any, Protocol, TypedDict
from urllib.parse import parse_qsl, quote, urlsplit

import httpx

if TYPE_CHECKING:
    from dlt.extract.source import DltSource

# Bump when allowlisted rendering or identity semantics change, so old cached
# rows cannot indefinitely bypass a new rendering policy on an unchanged tree.
_RENDER_VERSION = 3


class DocumentRow(TypedDict):
    id: str
    title: str
    content: str


class PostmanReader(Protocol):
    def get(self, path: str, **params: str | int) -> dict[str, Any]: ...


class PostmanSyncError(RuntimeError):
    """Sync was not authoritative; retain the previously loaded corpus."""


class PostmanClient:
    """Small read-only client. Errors deliberately omit response bodies and credentials."""

    def __init__(
        self,
        api_key: str,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        _validate_api_key(api_key)
        self._client = httpx.Client(
            base_url="https://api.postman.com",
            headers={
                "X-Api-Key": api_key,
                "Accept": "application/json",
                "User-Agent": "cognee-postman-connector/0.1",
            },
            timeout=30,
            transport=transport,
            follow_redirects=False,
        )
        self._sleep = sleep
        self._monthly_exhausted = False

    def close(self) -> None:
        self._client.close()

    def get(self, path: str, **params: str | int) -> dict[str, Any]:
        if self._monthly_exhausted:
            raise PostmanSyncError("Postman monthly API allowance exhausted.")
        for attempt in range(4):
            try:
                response = self._client.get(path, params=params)
            except httpx.TransportError:
                if attempt == 3:
                    raise PostmanSyncError("Postman transport failed; snapshot withheld.") from None
                self._sleep(2**attempt)
                continue
            remaining = response.headers.get("RateLimit-Remaining-Month")
            remaining = response.headers.get("X-RateLimit-Remaining-Month", remaining)
            self._monthly_exhausted = remaining == "0"
            if response.status_code == 429 or response.status_code >= 500:
                if self._monthly_exhausted or attempt == 3:
                    raise PostmanSyncError("Postman request allowance/retry budget exhausted.")
                delay = (
                    response.headers.get("Retry-After")
                    or response.headers.get("X-RateLimit-RetryAfter")
                    or response.headers.get("RetryAfter")
                )
                try:
                    delay = float(delay) if delay is not None else 2**attempt
                except ValueError:
                    delay = 2**attempt
                if not 0 <= delay <= 60:
                    raise PostmanSyncError("Postman retry requires a later sync.")
                self._sleep(delay)
                continue
            if response.status_code != 200:
                raise PostmanSyncError(f"Postman HTTP {response.status_code}; snapshot withheld.")
            try:
                payload = response.json()
            except ValueError:
                raise PostmanSyncError("Postman returned invalid JSON.") from None
            if not isinstance(payload, dict) or payload.get("error"):
                raise PostmanSyncError("Postman returned an invalid/error payload.")
            return payload
        raise PostmanSyncError("Postman request failed.")


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _description(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("content", "")
    return value if isinstance(value, str) else ""


def _secret_values(value: Any) -> set[str]:
    """Collect exact values from omitted auth/header/variable/body/example fields."""
    secrets = set()

    def strings(part):
        if isinstance(part, str) and len(part) >= 4:
            secrets.add(part)
        elif isinstance(part, dict):
            for child in part.values():
                strings(child)
        elif isinstance(part, list):
            for child in part:
                strings(child)

    def walk(part):
        if isinstance(part, dict):
            for key, child in part.items():
                if key == "auth" and isinstance(child, dict):
                    for auth_key, auth_value in child.items():
                        if auth_key != "type":
                            if isinstance(auth_value, list):
                                for entry in auth_value:
                                    if isinstance(entry, dict):
                                        strings(entry.get("value"))
                            else:
                                strings(auth_value)
                elif key in {"variable", "cookie"} and isinstance(child, list):
                    for entry in child:
                        if isinstance(entry, dict):
                            strings(entry.get("value"))
                elif key == "body":
                    if isinstance(child, str):
                        strings(child)
                    elif isinstance(child, dict):
                        strings(child.get("raw"))
                        strings(child.get("graphql"))
                        for field in ("urlencoded", "formdata"):
                            for entry in child.get(field, []) or []:
                                if isinstance(entry, dict):
                                    strings(entry.get("value"))
                elif key == "exec":
                    strings(child)
                elif key in {"header", "query"} and isinstance(child, list):
                    for entry in child:
                        if isinstance(entry, dict):
                            strings(entry.get("value"))
                elif key == "header" and isinstance(child, str):
                    strings(child)
                    for line in child.splitlines():
                        if ":" in line:
                            strings(line.split(":", 1)[1].strip())
                elif key == "url" or (key == "request" and isinstance(child, str)):
                    raw = child.get("raw", "") if isinstance(child, dict) else child
                    if isinstance(raw, str):
                        try:
                            parsed = urlsplit(raw)
                            strings(parsed.password)
                            for _, query_value in parse_qsl(parsed.query):
                                strings(query_value)
                        except ValueError:
                            pass
                    walk(child)
                else:
                    walk(child)
        elif isinstance(part, list):
            for child in part:
                walk(child)

    walk(value)
    return secrets


def flatten_collection(
    collection: dict[str, Any],
    uid: str,
    namespace: str,
    *,
    redact_values: Iterable[str] = (),
) -> list[DocumentRow]:
    """Allowlist documentation. Missing/duplicate IDs use positional identities.

    Fallback identities change on reorder/move; replace removes their old rows.
    URLs, auth, scripts, values, bodies and examples are never rendered.
    """
    if not isinstance(collection, dict) or not isinstance(collection.get("info"), dict):
        raise PostmanSyncError("Malformed collection info.")
    secrets = _secret_values(collection) | {s for s in redact_values if s}

    def clean(value):
        text = _description(value)
        for secret in sorted(secrets, key=len, reverse=True):
            text = text.replace(secret, "[REDACTED]")
        text = re.sub(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+", "[REDACTED]", text)
        text = re.sub(r"PMAK-[A-Za-z0-9-]+", "[REDACTED]", text)
        text = re.sub(
            r"(?i)\b(password|secret|token|api[_-]?key)\s*[:=]\s*[^\s,;]+",
            r"\1=[REDACTED]",
            text,
        )
        return text

    found = []

    def walk(items, path, indexes, docs, depth=0):
        if depth > 100 or not isinstance(items, list):
            raise PostmanSyncError("Collection nesting/shape is unsupported; snapshot withheld.")
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise PostmanSyncError("Malformed collection item.")
            position = indexes + [index]
            breadcrumb = path + [clean(item.get("name", ""))]
            if "request" in item:
                request = item["request"]
                if isinstance(request, str):
                    request = {"method": "GET", "url": request}
                if not isinstance(request, dict):
                    raise PostmanSyncError("Malformed request object; snapshot withheld.")
                found.append((item, request, breadcrumb, position, docs))
            elif "item" in item:
                walk(
                    item["item"],
                    breadcrumb,
                    position,
                    docs + [clean(item.get("description", ""))],
                    depth + 1,
                )
            else:
                raise PostmanSyncError("Collection item has neither request nor children.")

    info = collection["info"]
    walk(
        collection.get("item"),
        [clean(info.get("name", ""))],
        [],
        [clean(info.get("description", ""))],
    )
    ids = Counter(item.get("id") for item, *_ in found if isinstance(item.get("id"), str))
    rows = []
    for item, request, path, position, docs in found:
        item_id = item.get("id")
        if item_id is not None and not isinstance(item_id, str):
            raise PostmanSyncError("Invalid request item ID; snapshot withheld.")
        identity = ["item", item_id] if item_id and ids[item_id] == 1 else ["position", position]
        method = request.get("method", "GET")
        if not isinstance(method, str) or not re.fullmatch(r"[A-Z]{1,20}", method):
            raise PostmanSyncError("Invalid request method.")
        title = " / ".join(path)
        content = "\n\n".join(
            filter(
                None,
                [
                    f"Postman collection: {uid}",
                    f"Request: {title}",
                    f"Method: {method}",
                    *docs,
                    clean(item.get("description", "")),
                    clean(request.get("description", "")),
                ],
            )
        )
        rows.append({"id": _digest([namespace, uid, identity]), "title": title, "content": content})
    return sorted(rows, key=lambda row: row["id"])


def _inventory(
    client: PostmanReader,
    workspace_id: str,
    page_size: int,
    max_pages: int,
) -> dict[str, dict[str, str]]:
    result = {}
    total = None
    offset = 0
    for _ in range(max_pages):
        payload = client.get("/collections", workspace=workspace_id, limit=page_size, offset=offset)
        items, meta = payload.get("collections"), payload.get("meta")
        if not isinstance(items, list) or not isinstance(meta, dict):
            raise PostmanSyncError("Paginated collection inventory lacks items/meta.")
        count = meta.get("total")
        if type(count) is not int or count < 0 or meta.get("offset") != offset:
            raise PostmanSyncError("Invalid pagination metadata.")
        if total is not None and total != count:
            raise PostmanSyncError("Inventory changed during pagination.")
        total = count
        for item in items:
            if not isinstance(item, dict):
                raise PostmanSyncError("Malformed inventory entry.")
            uid, updated = item.get("uid"), item.get("updatedAt")
            if not isinstance(uid, str) or not uid or not isinstance(updated, str) or not updated:
                raise PostmanSyncError("Inventory entry lacks UID/update cursor.")
            if uid in result:
                raise PostmanSyncError("Repeated collection across inventory pages.")
            result[uid] = {"updatedAt": updated, "owner": str(item.get("owner", ""))}
        offset += len(items)
        if offset == total:
            return result
        if not items or offset > total:
            raise PostmanSyncError("Incomplete inventory.")
    raise PostmanSyncError("Inventory page budget exceeded.")


def build_snapshot(
    client: PostmanReader,
    state: dict[str, Any],
    *,
    workspace_id: str,
    account_id: str,
    namespace: str,
    missing_collection_policy: str = "require_confirmation",
    collection_ids: Iterable[str] | None = None,
    confirmed_deleted: Iterable[str] = (),
    page_size: int = 100,
    max_pages: int = 100,
    redact_values: Iterable[str] = (),
) -> tuple[list[DocumentRow], dict[str, Any]]:
    """Build privately, validating before yielding or modifying durable dlt state."""

    def validate_workspace():
        workspace = client.get(f"/workspaces/{quote(workspace_id, safe='')}").get("workspace")
        if not isinstance(workspace, dict) or workspace.get("id") != workspace_id:
            raise PostmanSyncError("Workspace identity/access could not be verified.")

    redact_values = tuple(sorted(set(redact_values)))
    render_policy = _digest([_RENDER_VERSION, list(redact_values)])
    validate_workspace()
    previous_inventory = None
    inventory = None
    for _ in range(3):
        current = _inventory(client, workspace_id, page_size, max_pages)
        if current == previous_inventory:
            inventory = current
            break
        previous_inventory = current
    if inventory is None:
        raise PostmanSyncError("Inventory failed to converge; snapshot withheld.")
    selected = set(inventory) if collection_ids is None else set(collection_ids)
    confirmed = set(confirmed_deleted)
    if confirmed & set(inventory):
        raise PostmanSyncError("Confirmed removal is still visible; refusing to remove it.")
    cached = copy.deepcopy(state.get("collections", {}))
    acknowledged = set(state.get("removed_collections", []))
    missing = (set(cached) | selected) - set(inventory)
    allowed = confirmed | acknowledged
    if missing_collection_policy == "mirror_visibility":
        allowed |= set(cached)
    if missing - allowed:
        raise PostmanSyncError(
            "Collection disappeared or lost access. "
            "Confirm deletion/removal explicitly before sync."
        )
    fresh = {}
    for uid in sorted(selected & set(inventory)):
        entry = inventory[uid]
        old = cached.get(uid)
        if (
            old
            and old["updatedAt"] == entry["updatedAt"]
            and old.get("render_policy") == render_policy
        ):
            fresh[uid] = old
            continue
        collection = client.get(f"/collections/{quote(uid, safe='')}").get("collection")
        fresh[uid] = {
            "updatedAt": entry["updatedAt"],
            "render_policy": render_policy,
            "rows": flatten_collection(collection, uid, namespace, redact_values=redact_values),
        }
    # A changed tree fetched while enumeration was stable can still race an edit.
    # Revalidate the inventory after fetching before publishing any deletions.
    if _inventory(client, workspace_id, page_size, max_pages) != inventory:
        raise PostmanSyncError("Inventory changed during tree fetch; retry sync.")
    validate_workspace()
    rows = [row for uid in sorted(fresh) for row in fresh[uid]["rows"]]
    next_state = {"collections": fresh}
    removed = (acknowledged | (missing & allowed)) - set(inventory)
    if removed:
        next_state["removed_collections"] = sorted(removed)
    return rows, next_state


def _validate_api_key(api_key: str) -> None:
    if (
        not isinstance(api_key, str)
        or not api_key.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in api_key)
    ):
        raise ValueError("API key must be nonempty text without control characters.")


def _validate_selection(values: Iterable[str], name: str) -> list[str]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be an iterable of UIDs, not a string.")
    entries = list(values)
    if any(
        not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value)
        for value in entries
    ):
        raise ValueError(f"{name} entries must be nonempty collection UIDs.")
    return sorted(set(entries))


def postman_source(
    api_key: str | None = None,
    workspace_id: str | None = None,
    collection_ids: Iterable[str] | None = None,
    *,
    account_id: str,
    corpus_owner: str,
    dataset_name: str,
    confirmed_deleted: Iterable[str] = (),
    missing_collection_policy: str = "require_confirmation",
    page_size: int = 100,
    max_pages: int = 100,
    client: PostmanReader | None = None,
    redact_values: Iterable[str] = (),
) -> "DltSource":
    """Return a scoped dlt source for foreground Cognee replace ingestion.

    ``account_id`` must match /me. Scope/owner/dataset and selection are immutable
    namespace inputs. Token rotation does not change identity. ``confirmed_deleted``
    acknowledges collection deletion or intentional scope removal after access loss.
    """
    import dlt
    from cognee.tasks.ingestion import dlt_utils

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError("Postman requires Cognee document sync version >= 1.")
    workspace_id = workspace_id or os.getenv("POSTMAN_WORKSPACE_ID")
    api_key = api_key or os.getenv("POSTMAN_API_KEY")
    if not all(
        isinstance(value, str) and value.strip() and value == value.strip()
        for value in [workspace_id, account_id, corpus_owner, dataset_name]
    ):
        raise ValueError("workspace_id, account_id, corpus_owner and dataset_name are required.")
    if client is None and not api_key:
        raise ValueError("Set POSTMAN_API_KEY locally or pass api_key.")
    if (
        type(page_size) is not int
        or type(max_pages) is not int
        or not 1 <= page_size <= 100
        or max_pages < 1
    ):
        raise ValueError("page_size must be 1..100 and max_pages positive.")
    if api_key is not None:
        _validate_api_key(api_key)
    if missing_collection_policy not in {"require_confirmation", "mirror_visibility"}:
        raise ValueError("Unknown missing_collection_policy.")
    selection = (
        None if collection_ids is None else _validate_selection(collection_ids, "collection_ids")
    )
    confirmed_deleted = _validate_selection(confirmed_deleted, "confirmed_deleted")
    if isinstance(redact_values, (str, bytes)):
        raise ValueError("redact_values must be an iterable of strings, not a string.")
    redact_values = tuple(redact_values)
    if any(not isinstance(value, str) for value in redact_values):
        raise ValueError("redact_values entries must be strings.")
    namespace = _digest([account_id, workspace_id, corpus_owner, dataset_name, selection])
    name = f"postman_{namespace[:32]}"

    @dlt.resource(
        name=name,
        primary_key="id",
        write_disposition="replace",
        columns={key: {"data_type": "text"} for key in ("id", "title", "content")},
    )
    def requests() -> Iterator[list[DocumentRow]]:
        service = client or PostmanClient(api_key)
        try:
            user = service.get("/me").get("user")
            if not isinstance(user, dict) or str(user.get("id")) != account_id:
                raise PostmanSyncError("Postman account differs from configured namespace.")
            # dlt resets resource_state before every replace extraction. Source-level
            # state retains the collection cache and still participates in load commit.
            state = dlt.current.source_state().setdefault("postman_snapshot", {})
            rows, next_state = build_snapshot(
                service,
                state,
                workspace_id=workspace_id,
                account_id=account_id,
                namespace=namespace,
                missing_collection_policy=missing_collection_policy,
                collection_ids=selection,
                confirmed_deleted=confirmed_deleted,
                page_size=page_size,
                max_pages=max_pages,
                redact_values=(*redact_values, api_key or ""),
            )
            # Empty list + explicit columns creates a zero-row replacement table.
            # The whole cache is re-emitted on rerun, including downstream retries.
            yield rows
            state.clear()
            state.update(next_state)
        finally:
            if client is None:
                service.close()

    @dlt.source(name=name)
    def source():
        return requests

    result = source()
    setattr(result, dlt_utils.DOCUMENT_SOURCE_ATTR, "postman")
    setattr(result, dlt_utils.PIPELINE_SCOPE_ATTR, name)
    return result
