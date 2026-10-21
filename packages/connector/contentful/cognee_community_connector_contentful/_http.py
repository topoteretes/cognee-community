"""Environment-scoped Delivery requests and validated collection traversal."""

import hashlib
import json
import re
import time
from urllib.parse import parse_qsl, urljoin, urlsplit

import httpx

from .exceptions import (
    ContentfulAPIError,
    ContentfulAuthError,
    ContentfulRateLimitError,
    ContentfulResponseError,
    ContentfulSyncTokenError,
)

_HOSTS = {"cdn.contentful.com", "cdn.eu.contentful.com"}
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.-]+$")
_ATTEMPTS = 5
_TIMEOUT = 30.0


def identifier(value, label):
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value) or value in {".", ".."}:
        raise ValueError(f"Contentful {label} must be a nonempty identifier.")
    return value


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class DeliveryClient:
    def __init__(self, host, space, environment, token, service, check_active=None, stats=None):
        if host not in _HOSTS:
            raise ValueError("Contentful host must be cdn.contentful.com or cdn.eu.contentful.com.")
        self.host = host
        self.space = identifier(space, "space_id")
        self.environment = identifier(environment, "environment")
        self.base = f"https://{host}/spaces/{space}/environments/{environment}/"
        self.space_path = f"/spaces/{space}/"
        self.headers = {"Authorization": f"Bearer {token}"}
        self.client = service
        self.owned = service is None
        self.check_active = check_active
        self.stats = stats if stats is not None else {}

    def __enter__(self):
        if self.owned:
            self.client = httpx.Client(timeout=_TIMEOUT, follow_redirects=False)
        return self

    def __exit__(self, *_args):
        if self.owned:
            self.client.close()

    def get(self, endpoint, params=None, *, missing=False):
        for attempt in range(_ATTEMPTS):
            if self.check_active is not None:
                self.check_active()
            try:
                response = self.client.get(
                    self.base + endpoint,
                    params=params,
                    headers=self.headers,
                    timeout=_TIMEOUT,
                    follow_redirects=False,
                )
            except httpx.TransportError:
                if attempt == _ATTEMPTS - 1:
                    raise ContentfulAPIError(
                        "Contentful request failed after bounded retries."
                    ) from None
                time.sleep(2**attempt)
                continue
            status = response.status_code
            if status == 429:
                self.stats["failed_rate_limit"] = self.stats.get("failed_rate_limit", 0) + 1
            if status in (401, 403):
                self.stats["failed_auth"] = self.stats.get("failed_auth", 0) + 1
                raise ContentfulAuthError(f"Contentful authentication failed (HTTP {status}).")
            if status in (429, 500, 502, 503, 504) and attempt < _ATTEMPTS - 1:
                reset = response.headers.get("X-Contentful-RateLimit-Reset")
                try:
                    delay = float(reset) if reset is not None else float(2**attempt)
                except ValueError:
                    delay = float(2**attempt)
                if not 0 <= delay <= 60:
                    error = ContentfulRateLimitError if status == 429 else ContentfulAPIError
                    raise error("Contentful requested a retry beyond the retry budget.")
                time.sleep(delay)
                continue
            if missing and status == 404:
                try:
                    error = response.json()
                except ValueError:
                    error = None
                system = error.get("sys") if isinstance(error, dict) else None
                if isinstance(system, dict) and system.get("id") == "NotFound":
                    return None
            if status != 200:
                if endpoint == "sync" and status in (400, 410) and (params or {}).get("sync_token"):
                    raise ContentfulSyncTokenError("Contentful rejected the Sync token.")
                error = ContentfulRateLimitError if status == 429 else ContentfulAPIError
                raise error(f"Contentful Delivery request failed (HTTP {status}).")
            try:
                payload = response.json()
            except ValueError:
                raise ContentfulResponseError("Contentful returned invalid JSON.") from None
            if not isinstance(payload, dict):
                raise ContentfulResponseError("Contentful returned an invalid response object.")
            return payload
        raise ContentfulAPIError("Contentful request exhausted its retry budget.")

    def continuation(self, value):
        if not isinstance(value, str) or not value:
            raise ContentfulResponseError("Contentful returned an invalid continuation.")
        try:
            parsed = urlsplit(urljoin(self.base + "sync", value))
            port = parsed.port
        except ValueError:
            raise ContentfulResponseError("Contentful returned an unsafe continuation.") from None
        if (
            parsed.scheme != "https"
            or parsed.hostname != self.host
            or port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed.path not in {urlsplit(self.base + "sync").path, self.space_path + "sync"}
        ):
            raise ContentfulResponseError("Contentful returned an unsafe continuation.")
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        if len(pairs) != 1 or pairs[0][0] != "sync_token" or not pairs[0][1]:
            raise ContentfulResponseError("Contentful returned an invalid Sync continuation.")
        return {"sync_token": pairs[0][1]}

    def validate_item(self, item, kinds):
        if not isinstance(item, dict) or not isinstance(item.get("sys"), dict):
            raise ContentfulResponseError("Contentful returned invalid resource metadata.")
        system = item["sys"]
        if not isinstance(system.get("type"), str) or system["type"] not in kinds:
            raise ContentfulResponseError("Contentful returned an unsupported resource kind.")
        try:
            identifier(system.get("id"), "resource ID")
        except ValueError:
            raise ContentfulResponseError("Contentful returned an invalid resource ID.") from None
        for key, expected in (("space", self.space), ("environment", self.environment)):
            if key in system:
                link = system[key]
                link_sys = link.get("sys") if isinstance(link, dict) else None
                if not isinstance(link_sys, dict) or link_sys.get("id") != expected:
                    raise ContentfulResponseError(
                        "Contentful resource scope differs from the configured space/environment. "
                        "Use a concrete environment ID, not a retargetable alias."
                    )
        if system["type"] == "ContentType":
            fields = item.get("fields")
            if not isinstance(fields, list):
                raise ContentfulResponseError("Contentful returned invalid content-model fields.")
            field_ids = set()
            for field in fields:
                if not isinstance(field, dict) or not isinstance(field.get("id"), str):
                    raise ContentfulResponseError("Contentful returned an invalid model field.")
                if not field["id"] or field["id"] in field_ids:
                    raise ContentfulResponseError(
                        "Contentful returned duplicate or empty model fields."
                    )
                field_ids.add(field["id"])

    @staticmethod
    def items(payload):
        items = payload.get("items")
        if not isinstance(items, list):
            raise ContentfulResponseError("Contentful returned an invalid collection.")
        return items

    def iter_sync(self, token):
        """Yield one validated page at a time, with the terminal token on its last page."""
        params = {"sync_token": token} if token else {"initial": "true", "limit": 100}
        seen = set()
        while True:
            marker = fingerprint(params)
            if marker in seen:
                raise ContentfulResponseError("Contentful repeated a Sync continuation.")
            seen.add(marker)
            payload = self.get("sync", params)
            items = self.items(payload)
            for item in items:
                self.validate_item(item, {"Entry", "Asset", "DeletedEntry", "DeletedAsset"})
            page, terminal = payload.get("nextPageUrl"), payload.get("nextSyncUrl")
            if bool(page) == bool(terminal):
                raise ContentfulResponseError(
                    "Contentful Sync must return one page or terminal continuation."
                )
            params = self.continuation(page or terminal)
            yield items, params["sync_token"] if terminal else None
            if terminal:
                return

    def model_inventory(self):
        previous = None
        for _pass in range(3):
            inventory = {}
            skip, expected_total = 0, None
            while True:
                payload = self.get("content_types", {"skip": skip, "limit": 100, "order": "sys.id"})
                total, offset, limit = (payload.get(key) for key in ("total", "skip", "limit"))
                if (
                    any(type(value) is not int for value in (total, offset, limit))
                    or total < 0
                    or offset != skip
                    or not 1 <= limit <= 100
                    or (expected_total is not None and total != expected_total)
                ):
                    raise ContentfulResponseError(
                        "Contentful returned inconsistent model pagination."
                    )
                expected_total = total
                items = self.items(payload)
                if len(items) != min(limit, max(0, total - skip)):
                    raise ContentfulResponseError(
                        "Contentful returned an incomplete model inventory."
                    )
                for item in items:
                    self.validate_item(item, {"ContentType"})
                    key = item["sys"]["id"]
                    if key in inventory:
                        raise ContentfulResponseError(
                            "Contentful duplicated a model ID within an inventory."
                        )
                    item["fields"] = sorted(item["fields"], key=lambda field: field["id"])
                    inventory[key] = item
                skip += len(items)
                if skip >= total:
                    break
            hashes = {key: fingerprint(item) for key, item in inventory.items()}
            if previous == hashes:
                return inventory, hashes
            previous = hashes
        raise ContentfulResponseError("Contentful model inventory changed during all three passes.")
