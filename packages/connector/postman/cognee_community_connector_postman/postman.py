"""Incremental Postman extraction with a complete, replayable document snapshot."""

import base64
import copy
import hashlib
import json
import re
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from typing import TYPE_CHECKING, Any, Protocol, TypedDict
from urllib.parse import parse_qsl, quote, unquote, urlsplit

import httpx
from cognee.shared.logging_utils import get_logger

_LOGGER = get_logger(__name__)

if TYPE_CHECKING:
    from dlt.extract.source import DltSource

# Bump when allowlisted rendering or identity semantics change, so old cached
# rows cannot indefinitely bypass a new rendering policy on an unchanged tree.
_RENDER_VERSION = 7
_MIN_PROSE_SECRET_LENGTH = 8


class DocumentRow(TypedDict):
    id: str
    title: str
    content: str
    url: str


class PostmanReader(Protocol):
    def get(self, path: str, **params: str | int) -> dict[str, Any]: ...


class PostmanSyncError(RuntimeError):
    """Sync was not authoritative; retain the previously loaded corpus."""


class PostmanAuthError(PostmanSyncError):
    """Account credentials or access were rejected."""


class PostmanRateLimitError(PostmanSyncError):
    """Retry/allowance budget requires a later sync."""


class PostmanDataError(PostmanSyncError):
    """Provider data is malformed or cannot form an authoritative snapshot."""


class PostmanClient:
    """Small read-only client. Errors deliberately omit response bodies and credentials."""

    def __init__(
        self,
        api_key: str,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        check_active: Callable[[], None] | None = None,
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
        self._check_active = check_active
        self._wait_remaining = 5.0

    def close(self) -> None:
        self._client.close()

    def _wait(self, delay: float, *, rate_limit: bool = False) -> None:
        # Shared across every request in this extraction, not reset per page/tree.
        if not 0 <= delay <= 2 or delay > self._wait_remaining:
            error = PostmanRateLimitError if rate_limit else PostmanSyncError
            raise error("Postman retry requires a later sync.")
        self._wait_remaining -= delay
        self._sleep(delay)

    def get(self, path: str, **params: str | int) -> dict[str, Any]:
        return self.get_checked(path, None, **params)

    def get_checked(self, path, check_active, **params):
        # One checkpoint coordinator, including retries; retain injected client hooks.
        checks = list({id(cb): cb for cb in (self._check_active, check_active) if cb}.values())
        if self._monthly_exhausted:
            raise PostmanRateLimitError("Postman monthly API allowance exhausted.")
        for attempt in range(4):
            for checkpoint in checks:
                checkpoint()
            try:
                response = self._client.get(path, params=params)
            except httpx.TransportError:
                if attempt == 3:
                    raise PostmanSyncError("Postman transport failed; snapshot withheld.") from None
                self._wait(2**attempt)
                continue
            remaining = response.headers.get("RateLimit-Remaining-Month")
            remaining = response.headers.get("X-RateLimit-Remaining-Month", remaining)
            self._monthly_exhausted = remaining == "0"
            if response.status_code == 429 or response.status_code >= 500:
                if self._monthly_exhausted or attempt == 3:
                    error = (
                        PostmanRateLimitError
                        if self._monthly_exhausted or response.status_code == 429
                        else PostmanSyncError
                    )
                    raise error("Postman request allowance/retry budget exhausted.")
                delay = (
                    response.headers.get("Retry-After")
                    or response.headers.get("X-RateLimit-RetryAfter")
                    or response.headers.get("RetryAfter")
                )
                try:
                    delay = float(delay) if delay is not None else 2**attempt
                except ValueError:
                    delay = 2**attempt
                self._wait(delay, rate_limit=response.status_code == 429)
                continue
            if response.status_code in (401, 403):
                raise PostmanAuthError("Postman authentication/access rejected.")
            if response.status_code != 200:
                raise PostmanSyncError(f"Postman HTTP {response.status_code}; snapshot withheld.")
            try:
                payload = response.json()
            except ValueError:
                raise PostmanDataError("Postman returned invalid JSON.") from None
            if not isinstance(payload, dict) or payload.get("error"):
                raise PostmanDataError("Postman returned an invalid/error payload.")
            return payload
        raise PostmanSyncError("Postman request failed.")


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _description(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("content", "")
    return value if isinstance(value, str) else ""


def _secret_values(value: Any) -> set[str]:
    """Exact auth/sensitive-key values only; ordinary metadata is not secret."""
    secrets = set()
    sensitive = re.compile(
        r"(?i)(authorization|(?:^|[-_])auth(?:$|[-_])|cookie|password|(?:^|[-_])(?:pwd|pass)(?:$|[-_])|secret|token|api[-_]?key|credential|signature|session|(?:^|[-_])sig(?:$|[-_])|(?:^|[-_])key$)"
    )

    def strings(part):
        if isinstance(part, str) and part:
            secrets.add(part)
            match = re.fullmatch(r"(?i)(?:bearer|basic|token)\s+(.+)", part)
            if match:
                secrets.add(match.group(1))
        elif isinstance(part, dict):
            for child in part.values():
                strings(child)
        elif isinstance(part, list):
            for child in part:
                strings(child)

    def token_like(text):
        if not isinstance(text, str):
            return False
        # Validate a three-part JWT header to avoid treating ordinary host names
        # as credentials; JSON whitespace/header ordering need not be canonical.
        if re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", text):
            try:
                header = text.split(".", 1)[0]
                decoded = json.loads(base64.urlsafe_b64decode(header + "=" * (-len(header) % 4)))
                if isinstance(decoded, dict) and isinstance(decoded.get("alg"), str):
                    return True
            except (ValueError, UnicodeError):
                pass
        # Hex tokens need not be mixed case. Long opaque values exclude dates,
        # regions and word-separated enums. Both are heuristics, not secret proofs.
        return bool(
            re.fullmatch(r"[A-Fa-f0-9]{32,}", text)
            or (
                len(text) >= 24
                and re.fullmatch(r"[A-Za-z0-9+/=]+", text)
                and re.search(r"[a-z]", text)
                and re.search(r"[A-Z]", text)
                and re.search(r"[0-9]", text)
            )
        )

    def text_values(text):
        # Shared fallback for raw text/form bodies and URL components. Never render bodies.
        for name, val in parse_qsl(text, keep_blank_values=True):
            if sensitive.search(name) or name.casefold() == "code" or token_like(val):
                strings(val)
        for match in re.finditer(
            r"([A-Za-z_][A-Za-z0-9_-]*)[\"']?\s*[:=]\s*[\"']?([^\s&;,\"'<>]+)", text
        ):
            if sensitive.search(match[1]) or match[1].casefold() == "code":
                strings(match[2])
        for match in re.finditer(
            r"(?i)\bauthorization\s*:\s*(?:bearer|basic|token)\s+([A-Za-z0-9._~+/=-]+)", text
        ):
            strings(match[1])
        urls = [text, *(match[0] for match in re.finditer(r"(?:https?://|//)[^\s\"'<>]+", text))]
        for candidate in urls:
            try:
                parsed = urlsplit(candidate)
                strings(parsed.password)
                for name, val in parse_qsl(parsed.query):
                    if sensitive.search(name) or name.casefold() == "code" or token_like(val):
                        strings(val)
                for segment in parsed.path.split("/"):
                    decoded = unquote(segment)
                    if token_like(decoded):
                        strings(segment)
                        strings(decoded)
            except ValueError:
                continue
        for val in re.findall(r"[A-Za-z0-9_+/=.-]+", text):
            val = val.strip(".,")
            if token_like(val):
                strings(val)

    def body_values(part, private=False):
        # Payload keys are application data, never Postman schema dispatch keys.
        # A JSON field called header/query/auth must not bypass credential discovery.
        if isinstance(part, dict):
            field = str(part.get("key", ""))
            if "value" in part and (
                sensitive.search(field)
                or field.casefold() == "code"
                or part.get("type") == "secret"
            ):
                strings(part["value"])
            for name, child in part.items():
                child_private = bool(private or sensitive.search(name) or name.casefold() == "code")
                child_private = child_private and name.casefold() not in {
                    "type",
                    "key",
                    "in",
                    "name",
                    "description",
                }
                body_values(child, child_private)
        elif isinstance(part, list):
            for child in part:
                body_values(child, private)
        elif isinstance(part, str):
            text_values(part)
            if private or token_like(part):
                strings(part)

    auth_headers = set()

    def find_auth(part):
        if isinstance(part, dict):
            auth = part.get("auth")
            if isinstance(auth, dict) and isinstance(auth.get("apikey"), list):
                fields = {
                    entry.get("key"): entry.get("value")
                    for entry in auth["apikey"]
                    if isinstance(entry, dict) and isinstance(entry.get("key"), str)
                }
                if fields.get("in") == "header" and isinstance(fields.get("key"), str):
                    auth_headers.add(fields["key"].strip().casefold())
            for child in part.values():
                find_auth(child)
        elif isinstance(part, list):
            for child in part:
                find_auth(child)

    find_auth(value)

    def walk(part, auth=False, body=False):
        if body:
            body_values(part)
            return
        if isinstance(part, dict):
            # Postman key/value entries in auth, headers, variables and query.
            field = str(part.get("key", ""))
            if "value" in part and (
                auth
                or part.get("type") == "secret"
                or sensitive.search(field)
                or field.casefold() == "code"
                or field.strip().casefold() in auth_headers
            ):
                strings(part["value"])
            for key, child in part.items():
                if key == "auth":
                    if isinstance(child, dict):
                        for auth_key, auth_value in child.items():
                            if auth_key != "type":
                                if isinstance(auth_value, list):
                                    for entry in auth_value:
                                        if isinstance(entry, dict) and entry.get("key") not in {
                                            "key",
                                            "in",
                                        }:
                                            strings(entry.get("value"))
                                elif isinstance(auth_value, dict):
                                    walk(auth_value, True, body)
                                else:
                                    strings(auth_value)
                elif key == "query" and isinstance(child, list):
                    for entry in child:
                        if isinstance(entry, dict):
                            field, val = str(entry.get("key", "")), entry.get("value")
                            if (
                                sensitive.search(field)
                                or field.casefold() == "code"
                                or token_like(val)
                            ):
                                strings(val)
                elif key == "body" and isinstance(child, dict):
                    walk(child, body=True)
                    raw = child.get("raw")
                    if isinstance(raw, str):
                        try:
                            walk(json.loads(raw), body=True)
                        except ValueError:
                            text_values(raw)
                        except RecursionError:
                            raise PostmanDataError("Request body nesting is unsupported.") from None
                elif key == "header" and isinstance(child, list):
                    for entry in child:
                        if isinstance(entry, dict):
                            name = str(entry.get("key", "")).strip().casefold()
                            if (
                                sensitive.search(name)
                                or name in auth_headers
                                or token_like(entry.get("value"))
                            ):
                                strings(entry.get("value"))
                elif key == "path" and isinstance(child, (str, list)):
                    segments = child.split("/") if isinstance(child, str) else child
                    for segment in segments:
                        if isinstance(segment, str) and token_like(unquote(segment)):
                            strings(segment)
                            strings(unquote(segment))
                elif key == "cookie":
                    walk(child, True, body)
                elif key == "header" and isinstance(child, str):
                    for line in child.splitlines():
                        name, sep, val = line.partition(":")
                        if sep and (
                            sensitive.search(name.strip())
                            or name.strip().casefold() in auth_headers
                            or token_like(val.strip())
                        ):
                            strings(val.strip())
                elif key in {"url", "request", "raw"} and isinstance(child, str):
                    try:
                        parsed = urlsplit(child)
                        strings(parsed.password)
                        for segment in parsed.path.split("/"):
                            decoded = unquote(segment)
                            if token_like(decoded):
                                strings(segment)
                                strings(decoded)
                        for name, val in parse_qsl(parsed.query):
                            if (
                                sensitive.search(name)
                                or name.casefold() == "code"
                                or token_like(val)
                            ):
                                strings(val)
                    except ValueError:
                        pass
                elif (
                    key not in {"key", "type"} and sensitive.search(key) and isinstance(child, str)
                ):
                    strings(child)
                else:
                    walk(child, auth, body)
        elif isinstance(part, list):
            for child in part:
                walk(child, auth, body)

    walk(value)
    return secrets


def flatten_collection(
    collection: dict[str, Any],
    uid: str,
    namespace: str,
    *,
    redact_values: Iterable[str] = (),
    include_endpoint_metadata: bool = True,
) -> list[DocumentRow]:
    """Allowlist documentation. Missing/duplicate IDs use positional identities.

    Fallback identities change on reorder/move; replace removes their old rows.
    Unresolved endpoint templates may be rendered; auth, scripts, header/query
    values, bodies and examples are omitted. Redaction is best effort.
    """
    if not isinstance(collection, dict) or not isinstance(collection.get("info"), dict):
        raise PostmanDataError("Malformed collection info.")
    secrets = _secret_values(collection) | {s for s in redact_values if s}

    def clean(value):
        text = _description(value)
        for secret in sorted(secrets, key=len, reverse=True):
            # Short values cannot be distinguished from ordinary prose/host words.
            # Credential-bearing fields are omitted; explicit auth syntax is scrubbed below.
            if len(secret) >= _MIN_PROSE_SECRET_LENGTH:
                text = text.replace(secret, "[REDACTED]")
        text = re.sub(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9_-]+", "[REDACTED]", text)
        text = re.sub(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+", "[REDACTED]", text)
        text = re.sub(
            r"(?i)\bauthorization\s*:\s*token\s+[A-Za-z0-9._~+/=-]+",
            "Authorization: [REDACTED]",
            text,
        )
        text = re.sub(r"\bgh[pousr]_[A-Za-z0-9_]+", "[REDACTED]", text)
        text = re.sub(
            r"(?i)((?:https?:)?//hooks\.slack\.com(?::[0-9]+)?/services)/[^\s?#]+",
            r"\1/[REDACTED]",
            text,
        )
        text = re.sub(r"PMAK-[A-Za-z0-9-]+", "[REDACTED]", text)
        text = re.sub(
            r"(?i)\b(password|db[_-]?(?:pwd|pass)|secret|token|api[_-]?key)\s*[:=]\s*[^\s,;]+",
            r"\1=[REDACTED]",
            text,
        )
        return text

    found = []
    overviews = []
    provenance = "https://go.postman.co/collection/" + quote(uid, safe="")

    def endpoint(request):
        if not include_endpoint_metadata:
            return []
        value = request.get("url", "")
        raw = value.get("raw", "") if isinstance(value, dict) else value
        if isinstance(value, dict) and not raw:
            host, path = value.get("host", ""), value.get("path", "")
            if isinstance(host, list) and all(isinstance(part, str) for part in host):
                host = ".".join(host)
            if isinstance(path, list) and all(isinstance(part, str) for part in path):
                path = "/".join(path)
            protocol = value.get("protocol", "")
            if not all(isinstance(part, str) for part in (host, path, protocol)):
                raise PostmanDataError("Malformed structured endpoint.")
            raw = (protocol + "://" if protocol else "") + host + ("/" + path if path else "")
        if not isinstance(raw, str):
            return []
        # Never resolve variables; discard userinfo, query values and fragments.
        template = raw.split("?", 1)[0].split("#", 1)[0]
        if "://" in template:
            scheme, tail = template.split("://", 1)
            authority, sep, path = tail.partition("/")
            template = scheme + "://" + authority.rsplit("@", 1)[-1] + sep + path
        else:
            prefix = "//" if template.startswith("//") else ""
            authority, sep, path = template.removeprefix("//").partition("/")
            template = prefix + authority.rsplit("@", 1)[-1] + sep + path
        if not re.fullmatch(r"[A-Za-z0-9_~{}:/%.@+\-]*", template):
            template = "[unsupported endpoint template]"
        names = []
        if isinstance(value, dict):
            query = value.get("query", [])
            if not isinstance(query, list) or any(not isinstance(entry, dict) for entry in query):
                raise PostmanDataError("Malformed endpoint query list.")
            names.extend(entry.get("key", "") for entry in query)
        else:
            try:
                names.extend(name for name, _ in parse_qsl(urlsplit(raw).query))
            except ValueError:
                pass
        headers = request.get("header", [])
        header_names = (
            [entry.get("key", "") for entry in headers if isinstance(entry, dict)]
            if isinstance(headers, list)
            else []
        )

        def safe_names(entries):
            return ", ".join(
                clean(name)
                for name in entries
                if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name)
            )

        return [
            label + text
            for label, text in (
                ("Endpoint template: ", clean(template)),
                ("Query names: ", safe_names(names)),
                ("Header names: ", safe_names(header_names)),
            )
            if text
        ]

    def walk(items, path, indexes, docs, depth=0):
        if depth > 100 or not isinstance(items, list):
            raise PostmanDataError("Collection nesting/shape is unsupported; snapshot withheld.")
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise PostmanDataError("Malformed collection item.")
            position = indexes + [index]
            breadcrumb = path + [clean(item.get("name", ""))]
            if "request" in item:
                request = item["request"]
                if isinstance(request, str):
                    request = {"method": "GET", "url": request}
                if not isinstance(request, dict):
                    raise PostmanDataError("Malformed request object; snapshot withheld.")
                found.append((item, request, breadcrumb, position, docs))
            elif "item" in item:
                before = len(found)
                walk(
                    item.get("item", []),
                    breadcrumb,
                    position,
                    docs + [clean(item.get("description", ""))],
                    depth + 1,
                )
                if len(found) == before and clean(item.get("description", "")):
                    identity = (
                        ["folder", item.get("id"), position]
                        if isinstance(item.get("id"), str)
                        else ["folder_position", position]
                    )
                    overviews.append((identity, breadcrumb, docs + [clean(item["description"])]))
            else:
                raise PostmanDataError("Collection item has neither request nor children.")

    info = collection["info"]
    walk(
        collection.get("item", []),
        [clean(info.get("name", ""))],
        [],
        [clean(info.get("description", ""))],
    )
    ids = Counter(item.get("id") for item, *_ in found if isinstance(item.get("id"), str))
    rows = []
    if not found and clean(info.get("description", "")):
        overviews.append(
            (["collection_overview"], [clean(info.get("name", ""))], [clean(info["description"])])
        )
    folder_ids = Counter(identity[1] for identity, _, _ in overviews if identity[0] == "folder")
    for identity, path, docs in overviews:
        if identity[0] == "folder":
            identity = (
                ["folder", identity[1]]
                if folder_ids[identity[1]] == 1
                else ["folder_position", identity[2]]
            )
        rows.append(
            {
                "id": _digest([namespace, uid, identity]),
                "title": " / ".join(path),
                "content": "\n\n".join(["Postman documentation: " + " / ".join(path), *docs]),
                "url": provenance,
            }
        )
    for item, request, path, position, docs in found:
        item_id = item.get("id")
        if item_id is not None and not isinstance(item_id, str):
            raise PostmanDataError("Invalid request item ID; snapshot withheld.")
        identity = ["item", item_id] if item_id and ids[item_id] == 1 else ["position", position]
        method = request.get("method", "GET")
        if not isinstance(method, str) or not re.fullmatch(r"[A-Z]{1,20}", method):
            raise PostmanDataError("Invalid request method.")
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
                    *endpoint(request),
                ],
            )
        )
        rows.append(
            {
                "id": _digest([namespace, uid, identity]),
                "title": title,
                "content": content,
                # Use provider-supplied request UID, never guess its creator from
                # the collection owner (collaborators may create the request).
                "url": (
                    "https://go.postman.co/request/" + quote(item["uid"], safe="")
                    if isinstance(item.get("uid"), str)
                    and re.fullmatch(
                        r"[0-9]+-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", item["uid"]
                    )
                    else provenance
                ),
            }
        )
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
            raise PostmanDataError("Paginated collection inventory lacks items/meta.")
        count = meta.get("total")
        if (
            isinstance(count, bool)
            or not isinstance(count, (int, float))
            or not 0 <= count < float("inf")
            or count != int(count)
            or meta.get("offset") != offset
        ):
            raise PostmanDataError("Invalid pagination metadata.")
        if total is not None and total != count:
            raise PostmanDataError("Inventory changed during pagination.")
        total = int(count)
        for item in items:
            if not isinstance(item, dict):
                raise PostmanDataError("Malformed inventory entry.")
            uid, updated = item.get("uid"), item.get("updatedAt")
            if not isinstance(uid, str) or not uid or not isinstance(updated, str) or not updated:
                raise PostmanDataError("Inventory entry lacks UID/update cursor.")
            if uid in result:
                raise PostmanDataError("Repeated collection across inventory pages.")
            result[uid] = {"updatedAt": updated, "owner": str(item.get("owner", ""))}
        offset += len(items)
        if offset == total:
            return result
        if not items or offset > total:
            raise PostmanDataError("Incomplete inventory.")
    raise PostmanDataError("Inventory page budget exceeded.")


def build_snapshot(
    client: PostmanReader,
    state: dict[str, Any],
    *,
    workspace_id: str,
    account_id: str,
    namespace: str,
    missing_collection_policy: str = "mirror_visibility",
    collection_ids: Iterable[str] | None = None,
    confirmed_deleted: Iterable[str] = (),
    page_size: int = 100,
    max_pages: int = 100,
    redact_values: Iterable[str] = (),
    force_refresh: bool = False,
    stats: dict[str, int] | None = None,
    max_unconfirmed_removals: int = 1,
    include_endpoint_metadata: bool = True,
) -> tuple[list[DocumentRow], dict[str, Any]]:
    """Build privately, validating before yielding or modifying durable dlt state."""

    def validate_workspace():
        workspace = client.get(f"/workspaces/{quote(workspace_id, safe='')}").get("workspace")
        if not isinstance(workspace, dict) or workspace.get("id") != workspace_id:
            raise PostmanSyncError("Workspace identity/access could not be verified.")

    redact_values = tuple(sorted(set(redact_values)))
    render_policy = _digest([_RENDER_VERSION, list(redact_values), include_endpoint_metadata])
    validate_workspace()
    inventory = _inventory(client, workspace_id, page_size, max_pages)
    selected = set(inventory) if collection_ids is None else set(collection_ids)
    confirmed = set(confirmed_deleted)
    # A stale confirmation must not suppress a collection that becomes visible again.
    confirmed -= set(inventory)
    cached = copy.deepcopy(state.get("collections", {}))
    acknowledged = set(state.get("removed_collections", []))
    # Deliberately deselected collections retire from this stable resource.
    missing = ((set(cached) | selected) if collection_ids is None else selected) - set(inventory)
    allowed = confirmed | acknowledged
    if missing_collection_policy == "mirror_visibility" and collection_ids is None:
        allowed |= set(cached)
    if missing - allowed:
        raise PostmanSyncError(
            "Collection disappeared or lost access. "
            "Confirm deletion/removal explicitly before sync: "
            + ", ".join(sorted(missing - allowed))
        )
    unconfirmed = (missing & set(cached)) - confirmed - acknowledged
    # Outstanding automatic removals never expire with wall time. A scheduled
    # sync must not get a fresh deletion budget merely because it ran hours later.
    pending_values = state.get("pending_removals", [])
    if not isinstance(pending_values, list) or any(
        not isinstance(uid, str) or not uid for uid in pending_values
    ):
        raise PostmanDataError("Malformed pending removal state; snapshot withheld.")
    pending = set(pending_values)
    if "pending_removals" not in state and collection_ids is None:
        # Old releases may have expired the timestamped history while keeping
        # acknowledgements. Treat that ambiguous history conservatively on upgrade.
        pending.update(acknowledged)
    legacy = state.get("automatic_removals", [])
    if not isinstance(legacy, list) or any(
        not isinstance(entry, dict) or not isinstance(entry.get("uid"), str) for entry in legacy
    ):
        raise PostmanDataError("Malformed legacy removal state; snapshot withheld.")
    pending.update(entry["uid"] for entry in legacy)
    if any(not isinstance(uid, str) for uid in pending):
        raise PostmanDataError("Malformed pending removal state; snapshot withheld.")
    pending -= set(inventory) | confirmed
    if collection_ids is not None:
        pending &= selected  # Explicit deselection is deliberate, not provider absence.
    if missing_collection_policy == "mirror_visibility" and unconfirmed:
        if not inventory or len(pending | unconfirmed) > max_unconfirmed_removals:
            raise PostmanSyncError(
                "Bulk collection removal requires confirmed_deleted; snapshot withheld. "
                "Unconfirmed UIDs: " + ", ".join(sorted(pending | unconfirmed))
            )
        pending |= unconfirmed
    fresh = {}
    fetched = reused = 0
    for uid in sorted(selected & set(inventory)):
        entry = inventory[uid]
        old = cached.get(uid)
        if (
            not force_refresh
            and old
            and old["updatedAt"] == entry["updatedAt"]
            and old.get("render_policy") == render_policy
        ):
            fresh[uid] = old
            reused += 1
            continue
        collection = client.get(f"/collections/{quote(uid, safe='')}").get("collection")
        fetched += 1
        fresh[uid] = {
            "updatedAt": entry["updatedAt"],
            "render_policy": render_policy,
            "rows": flatten_collection(
                collection,
                uid,
                namespace,
                redact_values=redact_values,
                include_endpoint_metadata=include_endpoint_metadata,
            ),
        }
    # A changed tree fetched while enumeration was stable can still race an edit.
    # Revalidate the inventory after fetching before publishing any deletions.
    if (fetched or set(cached) != set(fresh)) and _inventory(
        client, workspace_id, page_size, max_pages
    ) != inventory:
        raise PostmanSyncError("Inventory changed during tree fetch; retry sync.")
    rows = [row for uid in sorted(fresh) for row in fresh[uid]["rows"]]
    next_state = {"collections": fresh, "pending_removals": sorted(pending)}
    # Keep only acknowledgements needed to replay an explicit missing selection.
    removed = (
        (acknowledged | (missing & allowed)) & missing if collection_ids is not None else set()
    )
    if removed:
        next_state["removed_collections"] = sorted(removed)
    if stats is not None:
        stats.update(scanned=len(selected), skipped=reused, fetched=fetched, documents=len(rows))
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
    resource_name: str,
    account_id: str | None = None,
    corpus_owner: str | None = None,
    dataset_name: str | None = None,
    confirmed_deleted: Iterable[str] = (),
    missing_collection_policy: str = "mirror_visibility",
    page_size: int = 100,
    max_pages: int = 100,
    max_unconfirmed_removals: int = 1,
    client: PostmanReader | None = None,
    redact_values: Iterable[str] = (),
    force_refresh: bool = False,
    include_endpoint_metadata: bool = True,
    service: PostmanReader | None = None,
    check_active: Callable[[], None] | None = None,
) -> "DltSource":
    """Return a scoped dlt source for foreground Cognee replace ingestion.

    Optional ``account_id`` must match /me; otherwise the first loaded account is
    bound in durable state. Ownership fields/resource_name are stable namespace
    inputs; selection is mutable. Token rotation preserves identity.
    ``confirmed_deleted``
    acknowledges collection deletion or intentional scope removal after access loss.
    """
    import dlt
    from cognee.tasks.ingestion import dlt_utils

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError("Postman requires Cognee document sync version >= 1.")
    if service is not None and client is not None:
        raise ValueError("Pass service or client, not both.")
    client = service if service is not None else client
    if not all(
        isinstance(value, str) and value.strip() and value == value.strip()
        for value in [
            workspace_id,
            *[value for value in (account_id, corpus_owner, dataset_name) if value is not None],
        ]
    ):
        raise ValueError("workspace_id and any explicit ownership fields must be nonempty text.")
    if client is None and not api_key:
        raise ValueError("Pass api_key or an injected service.")
    if (
        type(page_size) is not int
        or type(max_pages) is not int
        or not 1 <= page_size <= 100
        or max_pages < 1
    ):
        raise ValueError("page_size must be 1..100 and max_pages positive.")
    if type(max_unconfirmed_removals) is not int or max_unconfirmed_removals < 0:
        raise ValueError("max_unconfirmed_removals must be a nonnegative integer.")
    if api_key is not None:
        _validate_api_key(api_key)
    if missing_collection_policy not in {"require_confirmation", "mirror_visibility"}:
        raise ValueError("Unknown missing_collection_policy.")
    if type(include_endpoint_metadata) is not bool:
        raise ValueError("include_endpoint_metadata must be a boolean.")
    if type(force_refresh) is not bool:
        raise ValueError("force_refresh must be a boolean.")
    selection = (
        None if collection_ids is None else _validate_selection(collection_ids, "collection_ids")
    )
    confirmed_deleted = _validate_selection(confirmed_deleted, "confirmed_deleted")
    if isinstance(redact_values, (str, bytes)):
        raise ValueError("redact_values must be an iterable of strings, not a string.")
    redact_values = tuple(redact_values)
    if any(not isinstance(value, str) for value in redact_values):
        raise ValueError("redact_values entries must be strings.")
    if not isinstance(resource_name, str) or not re.fullmatch(
        r"[a-z][a-z0-9_]{0,62}", resource_name
    ):
        raise ValueError("resource_name must be a lowercase identifier (1..63 characters).")

    class CheckedReader:
        def __init__(self, provider):
            self.provider = provider

        def get(self, path, **params):
            if isinstance(self.provider, PostmanClient):
                return self.provider.get_checked(path, check_active, **params)
            if check_active:
                check_active()
            return self.provider.get(path, **params)

    # Source/table names must be known before lazy extraction. Bind the first
    # verified account in transactional state instead of doing I/O in the factory.
    namespace = _digest([workspace_id, corpus_owner, dataset_name, resource_name])
    stats = dict(
        scanned=0,
        skipped=0,
        fetched=0,
        documents=0,
        failed=0,
        failed_auth=0,
        failed_rate_limit=0,
        failed_data=0,
    )
    name = f"postman_{namespace[:32]}"

    @dlt.resource(
        name=name,
        primary_key="id",
        write_disposition="replace",
        columns={key: {"data_type": "text"} for key in ("id", "title", "content", "url")},
    )
    def requests() -> Iterator[list[DocumentRow]]:
        stats.update({key: 0 for key in stats})
        provider = client or PostmanClient(api_key)
        reader = CheckedReader(provider)
        try:
            # dlt resets resource_state before every replace extraction. Source-level
            # state retains the collection cache and still participates in load commit.
            state = dlt.current.source_state().setdefault("postman_snapshot", {})
            user = reader.get("/me").get("user")
            identity = user.get("id") if isinstance(user, dict) else None
            if (
                isinstance(identity, bool)
                or not isinstance(identity, (str, int))
                or not str(identity)
            ):
                raise PostmanAuthError("Postman account identity could not be verified.")
            identity = str(identity)
            if account_id is not None and identity != account_id:
                raise PostmanAuthError("Postman account differs from configured namespace.")
            if state.get("account_id", identity) != identity:
                raise PostmanAuthError("Postman account changed; use a separate resource identity.")
            rows, next_state = build_snapshot(
                reader,
                state,
                workspace_id=workspace_id,
                account_id=identity,
                namespace=namespace,
                missing_collection_policy=missing_collection_policy,
                collection_ids=selection,
                confirmed_deleted=confirmed_deleted,
                page_size=page_size,
                max_pages=max_pages,
                max_unconfirmed_removals=max_unconfirmed_removals,
                redact_values=(*redact_values, api_key or ""),
                force_refresh=force_refresh,
                stats=stats,
                include_endpoint_metadata=include_endpoint_metadata,
            )
            next_state["account_id"] = identity
            # Empty list + explicit columns creates a zero-row replacement table.
            # The whole cache is re-emitted on rerun, including downstream retries.
            if check_active:
                check_active()
            yield rows
            if check_active:
                check_active()
            state.clear()
            state.update(next_state)
        except Exception as exc:
            stats["failed"] += 1
            _LOGGER.warning(
                "Postman extraction failed (%s); snapshot withheld.", type(exc).__name__
            )
            if isinstance(exc, PostmanAuthError):
                stats["failed_auth"] += 1
            elif isinstance(exc, PostmanRateLimitError):
                stats["failed_rate_limit"] += 1
            elif isinstance(exc, PostmanDataError):
                stats["failed_data"] += 1
            raise
        finally:
            if client is None:
                provider.close()

    @dlt.source(name=name)
    def source():
        return requests

    result = source()
    result.cognee_sync_stats = stats
    for resource in result.resources.values():
        resource.cognee_sync_stats = stats
    setattr(result, dlt_utils.DOCUMENT_SOURCE_ATTR, "postman")
    setattr(result, dlt_utils.PIPELINE_SCOPE_ATTR, name)
    return result
