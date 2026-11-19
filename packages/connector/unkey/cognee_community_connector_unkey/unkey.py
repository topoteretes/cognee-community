"""Unkey API key infrastructure data-source connector for Cognee.

Syncs APIs, key metadata, identities, and rate-limiting configurations from Unkey
into Cognee memory documents with incremental fingerprinting and deletion
tombstones (forget-on-delete).
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from typing import Any
from urllib.parse import urlparse

import dlt
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("unkey_connector")

DEFAULT_UNKEY_BASE_URL = "https://api.unkey.dev"
DEFAULT_DASHBOARD_URL = "https://app.unkey.com"
DEFAULT_PAGE_LIMIT = 50
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF_FACTOR = 0.5
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

VALID_INCLUDES = ("apis", "keys", "identities")
ROW_COLUMNS = ("id", "title", "content", "url", "_deleted")

UNKEY_REMEMBER_KWARGS: dict[str, Any] = {
    "primary_key": "id",
    "write_disposition": "merge",
}


class UnkeyError(Exception):
    """Base exception for all Unkey connector errors."""


class UnkeyAuthError(UnkeyError):
    """Raised when authentication fails (HTTP 401)."""


class UnkeyAccessError(UnkeyError):
    """Raised when access is forbidden (HTTP 403)."""


class UnkeyNotFoundError(UnkeyError):
    """Raised when an API, key, or identity is not found (HTTP 404)."""


class UnkeyRateLimitedError(UnkeyError):
    """Raised when the Unkey API rate limit is reached (HTTP 429)."""


class UnkeyAPIError(UnkeyError):
    """Raised on unexpected HTTP status or malformed response payload."""


class UnkeyClient:
    """Resilient HTTP client for the Unkey REST API with retry and cursor support."""

    def __init__(
        self,
        root_key: str,
        *,
        base_url: str = DEFAULT_UNKEY_BASE_URL,
        session: Any = None,
        retries: int = DEFAULT_RETRIES,
        backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
    ) -> None:
        self.root_key = self._validate_key(root_key)
        self.base_url = self._validate_base_url(base_url)
        self.retries = max(0, retries)
        self.backoff_factor = max(0.0, backoff_factor)

        if session is not None:
            self._session = session
        else:
            import requests

            self._session = requests.Session()

    @staticmethod
    def _validate_key(key: str) -> str:
        if not isinstance(key, str):
            raise ValueError(f"root_key must be a string, got {type(key).__name__}")
        cleaned = key.strip()
        if not cleaned or not cleaned.isascii() or not cleaned.isprintable() or " " in cleaned:
            raise ValueError("root_key must be a non-empty, printable ASCII string without spaces")
        return cleaned

    @staticmethod
    def _validate_base_url(url: str) -> str:
        if not isinstance(url, str):
            raise ValueError(f"base_url must be a string, got {type(url).__name__}")
        cleaned = url.strip().rstrip("/")
        parsed = urlparse(cleaned)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"base_url must be an absolute http(s) URL, got {url!r}")
        return cleaned

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = {
            "Authorization": f"Bearer {self.root_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        attempt = 0
        while True:
            try:
                resp = self._session.request(
                    method=method.upper(),
                    url=url,
                    headers=headers,
                    params=params,
                    json=json_body,
                    timeout=30,
                )
            except Exception as exc:
                if attempt >= self.retries:
                    raise UnkeyAPIError(
                        f"Unkey request to {path} failed after {attempt} retries: {exc}"
                    ) from exc
                attempt += 1
                time.sleep(self.backoff_factor * (2 ** (attempt - 1)))
                continue

            status = resp.status_code
            if status in (200, 201):
                try:
                    return resp.json()
                except Exception as exc:
                    raise UnkeyAPIError(f"Unkey {path} returned non-JSON payload") from exc

            if status in RETRYABLE_STATUSES and attempt < self.retries:
                attempt += 1
                retry_after = resp.headers.get("Retry-After")
                sleep_sec = (
                    float(retry_after)
                    if retry_after
                    else self.backoff_factor * (2 ** (attempt - 1))
                )
                time.sleep(sleep_sec)
                continue

            self._map_http_error(status, path, resp.text)

    @staticmethod
    def _map_http_error(status: int, path: str, body: str) -> None:
        if status == 401:
            raise UnkeyAuthError(f"Unkey authentication failed (HTTP 401) on {path}: {body}")
        if status == 403:
            raise UnkeyAccessError(f"Unkey access forbidden (HTTP 403) on {path}: {body}")
        if status == 404:
            raise UnkeyNotFoundError(f"Unkey resource not found (HTTP 404) on {path}: {body}")
        if status == 429:
            raise UnkeyRateLimitedError(f"Unkey rate limit exceeded (HTTP 429) on {path}: {body}")
        raise UnkeyAPIError(f"Unkey request to {path} failed with HTTP {status}: {body}")


def _text(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, (int, float, bool)):
        return str(val)
    return str(val).strip()


def _lines(pairs: Iterable[tuple[str, str]]) -> list[str]:
    return [f"{label}: {val}" for label, val in pairs if val]


def _hash(obj: Any) -> str:
    dumped = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()


def render_api(api: dict[str, Any], dashboard_url: str) -> dict[str, Any]:
    api_id = _text(api.get("id"))
    name = _text(api.get("name")) or f"API {api_id}"
    workspace_id = _text(api.get("workspaceId"))
    description = _text(api.get("description")) or "No description provided"
    created_at = _text(api.get("createdAt"))

    fields = [
        ("Type", "Unkey API"),
        ("Name", name),
        ("API ID", api_id),
        ("Workspace ID", workspace_id),
        ("Description", description),
        ("Created At", created_at),
    ]
    return {
        "id": f"api:{api_id}",
        "title": f"Unkey API: {name} ({api_id})",
        "content": "\n".join(_lines(fields)),
        "url": f"{dashboard_url}/apis/{api_id}",
        "_deleted": False,
    }


def render_key(key: dict[str, Any], api_id: str, dashboard_url: str) -> dict[str, Any]:
    key_id = _text(key.get("id"))
    name = _text(key.get("name")) or f"Key {key_id}"
    owner_id = _text(key.get("ownerId")) or "Unassigned"
    enabled = bool(key.get("enabled", True))
    status_str = "Enabled" if enabled else "Disabled"

    ratelimit = key.get("ratelimit") or {}
    rl_limit = ratelimit.get("limit")
    rl_duration = ratelimit.get("duration")
    rl_str = f"{rl_limit} per {rl_duration}ms" if rl_limit else "Unrestricted"

    remaining = _text(key.get("remaining"))
    expires = _text(key.get("expires"))

    fields = [
        ("Type", "Unkey API Key"),
        ("Key Name", name),
        ("Key ID", key_id),
        ("Parent API ID", api_id),
        ("Owner / Tenant ID", owner_id),
        ("Status", status_str),
        ("Rate Limit", rl_str),
        ("Remaining Quota", remaining if remaining else "Unlimited"),
        ("Expiration", expires if expires else "Never"),
        ("Key Secret / Plaintext", "[Redacted & Protected]"),
        ("Key Hash", "[Redacted & Protected]"),
    ]
    return {
        "id": f"key:{api_id}:{key_id}",
        "title": f"Unkey Key: {name} [{status_str}]",
        "content": "\n".join(_lines(fields)),
        "url": f"{dashboard_url}/apis/{api_id}/keys/{key_id}",
        "_deleted": False,
    }


def render_identity(ident: dict[str, Any], dashboard_url: str) -> dict[str, Any]:
    ident_id = _text(ident.get("id"))
    ext_id = _text(ident.get("externalId")) or ident_id

    fields = [
        ("Type", "Unkey Identity"),
        ("External ID", ext_id),
        ("Identity ID", ident_id),
    ]
    return {
        "id": f"identity:{ident_id}",
        "title": f"Unkey Identity: {ext_id} ({ident_id})",
        "content": "\n".join(_lines(fields)),
        "url": f"{dashboard_url}/identities/{ident_id}",
        "_deleted": False,
    }


class _UnkeyStateWalker:
    def __init__(
        self,
        client: Any,
        dashboard_url: str,
        state: dict[str, Any],
        stats: dict[str, int],
        *,
        include: Sequence[str] = VALID_INCLUDES,
    ) -> None:
        self.client = client
        self.dashboard_url = dashboard_url.rstrip("/")
        self.state = state
        self.stats = stats
        self.include = tuple(include)

        self.fingerprints: dict[str, str] = self.state.setdefault("fingerprints", {})
        self.seen: list[str] = []

    def rows(self) -> Iterator[dict[str, Any]]:
        self.stats.setdefault("scanned", 0)
        self.stats.setdefault("emitted", 0)
        self.stats.setdefault("skipped", 0)
        self.stats.setdefault("deleted", 0)
        self.stats.setdefault("failed", 0)

        # 1. APIs and Keys
        apis: list[dict[str, Any]] = []
        if "apis" in self.include or "keys" in self.include:
            apis = list(self._walk_apis())
            if "apis" in self.include:
                for a in apis:
                    row = render_api(a, self.dashboard_url)
                    fp = _hash(row)
                    if self._changed(row["id"], fp):
                        yield self._emit(row, fp)

        if "keys" in self.include:
            for a in apis:
                api_id = str(a["id"])
                for k in self._walk_keys(api_id):
                    row = render_key(k, api_id, self.dashboard_url)
                    fp = _hash(row)
                    if self._changed(row["id"], fp):
                        yield self._emit(row, fp)

        # 2. Identities
        if "identities" in self.include:
            for idn in self._walk_identities():
                row = render_identity(idn, self.dashboard_url)
                fp = _hash(row)
                if self._changed(row["id"], fp):
                    yield self._emit(row, fp)

        # 3. Reconcile deletions
        self._close_cycle()
        yield from self._forget()

    def _walk_apis(self) -> Iterator[dict[str, Any]]:
        res = self.client.request("GET", "v1/apis.listApis") or {}
        items = res.get("apis") or []
        yield from items

    def _walk_keys(self, api_id: str) -> Iterator[dict[str, Any]]:
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"apiId": api_id, "limit": DEFAULT_PAGE_LIMIT}
            if cursor:
                params["cursor"] = cursor
            res = self.client.request("GET", "v1/apis.listKeys", params=params) or {}
            items = res.get("keys") or []
            yield from items
            cursor = res.get("cursor")
            if not cursor or len(items) == 0:
                break

    def _walk_identities(self) -> Iterator[dict[str, Any]]:
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": DEFAULT_PAGE_LIMIT}
            if cursor:
                params["cursor"] = cursor
            res = (
                self.client.request(
                    "GET",
                    "v1/identities.listIdentities",
                    params=params,
                )
                or {}
            )
            items = res.get("identities") or []
            yield from items
            cursor = res.get("cursor")
            if not cursor or len(items) == 0:
                break

    def _changed(self, row_id: str, fingerprint: str) -> bool:
        self.seen.append(row_id)
        self.stats["scanned"] += 1
        if self.fingerprints.get(row_id) == fingerprint:
            self.stats["skipped"] += 1
            return False
        return True

    def _emit(self, row: dict[str, Any], fingerprint: str) -> dict[str, Any]:
        self.fingerprints[row["id"]] = fingerprint
        self.stats["emitted"] += 1
        return row

    def _close_cycle(self) -> None:
        missing = set(self.fingerprints) - set(self.seen)
        self.state["pending_deletes"] = sorted(missing)
        self.seen.clear()

    def _forget(self) -> Iterator[dict[str, Any]]:
        pending = self.state.setdefault("pending_deletes", [])
        while pending:
            row_id = pending[0]
            try:
                gone = self._is_gone(row_id)
            except (UnkeyAPIError, UnkeyAccessError) as exc:
                self.stats["failed"] += 1
                logger.warning("Unkey deletion check failed, will retry next run: %s", exc)
                gone = False

            if gone:
                self.fingerprints.pop(row_id, None)
                self.stats["deleted"] += 1
                yield {"id": row_id, "_deleted": True}
            pending.pop(0)

    def _is_gone(self, row_id: str) -> bool:
        parts = row_id.split(":")
        prefix = parts[0]
        if prefix == "api":
            if "apis" not in self.include:
                return True
            api_id = parts[1]
            try:
                self.client.request("GET", "v1/apis.getApi", params={"apiId": api_id})
                return False
            except UnkeyNotFoundError:
                return True
        elif prefix == "key":
            if "keys" not in self.include:
                return True
            key_id = parts[2]
            try:
                self.client.request("GET", "v1/keys.getKey", params={"keyId": key_id})
                return False
            except UnkeyNotFoundError:
                return True
        elif prefix == "identity":
            if "identities" not in self.include:
                return True
            ident_id = parts[1]
            try:
                self.client.request(
                    "GET",
                    "v1/identities.getIdentity",
                    params={"identityId": ident_id},
                )
                return False
            except UnkeyNotFoundError:
                return True
        return True


def _iter_rows(
    client: Any,
    dashboard_url: str,
    state: dict[str, Any],
    stats: dict[str, int],
    **kwargs: Any,
) -> Iterator[dict[str, Any]]:
    walker = _UnkeyStateWalker(client, dashboard_url, state, stats, **kwargs)
    yield from walker.rows()


def unkey_source(
    *,
    resource_name: str,
    root_key: str | None = None,
    base_url: str = DEFAULT_UNKEY_BASE_URL,
    dashboard_url: str = DEFAULT_DASHBOARD_URL,
    service: Any = None,
    check_active: Callable[[], None] | None = None,
    include: Sequence[str] = VALID_INCLUDES,
):
    """Return a dlt resource yielding Unkey API keys and infrastructure for cognee.remember."""
    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError("Unkey sync requires cognee>=1.6.3 (table-scoped document cleanup).")
    if not isinstance(resource_name, str) or not resource_name.strip():
        raise ValueError("unkey_source needs a valid resource_name")

    include = tuple(include)
    if not include or set(include) - set(VALID_INCLUDES):
        raise ValueError(f"include must be a non-empty subset of {VALID_INCLUDES}")

    client = (
        service if service is not None else UnkeyClient(root_key=root_key or "", base_url=base_url)
    )
    resolved_dash = (dashboard_url or DEFAULT_DASHBOARD_URL).rstrip("/")
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def unkey_workspace():
        stats.clear()
        stats.update(scanned=0, skipped=0, deleted=0, failed=0)
        rows = _iter_rows(
            client=client,
            dashboard_url=resolved_dash,
            state=dlt.current.resource_state(),
            stats=stats,
            include=include,
        )
        yield from dlt_utils.guarded_rows(rows, check_active)

    res = unkey_workspace()
    setattr(res, DOCUMENT_SOURCE_ATTR, "unkey")
    setattr(res, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    res.cognee_sync_stats = stats
    return res


unkey = unkey_source
