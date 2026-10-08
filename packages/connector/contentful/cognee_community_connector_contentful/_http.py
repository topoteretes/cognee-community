"""Private Delivery transport: scoped requests, validated cursors and inventories."""

import hashlib
import json
import logging
import re
import time
from urllib.parse import parse_qsl, quote, urljoin, urlsplit

import httpx

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


class _RedactRequests(logging.Filter):
    def __init__(self, secrets):
        super().__init__()
        self.secrets = secrets

    def filter(self, record):
        message = record.getMessage()
        for secret in tuple(self.secrets):
            message = message.replace(secret, "[redacted]")
            message = message.replace(quote(secret, safe=""), "[redacted]")
        message = re.sub(
            r"((?:access_token|sync_token|pageNext)=)[^&\s\"']+", r"\1[redacted]", message
        )
        record.msg, record.args = message, ()
        return True


class DeliveryClient:
    def __init__(self, host, space, environment, token, client):
        if host not in _HOSTS:
            raise ValueError("Contentful host must be cdn.contentful.com or cdn.eu.contentful.com.")
        self.host = host
        self.space = identifier(space, "space_id")
        self.environment = identifier(environment, "environment")
        self.base = f"https://{host}/spaces/{space}/environments/{environment}/"
        self.space_path = f"/spaces/{space}/"
        self.secrets = {token}
        self.headers = {"Authorization": f"Bearer {token}"}
        self.client = client
        self.owned = client is None
        self.filter = _RedactRequests(self.secrets)

    def __enter__(self):
        if self.owned:
            self.client = httpx.Client(timeout=_TIMEOUT, follow_redirects=False)
        logging.getLogger("httpx").addFilter(self.filter)
        return self

    def __exit__(self, *_args):
        logging.getLogger("httpx").removeFilter(self.filter)
        if self.owned:
            self.client.close()

    def get(self, endpoint, params=None, *, missing=False):
        for attempt in range(_ATTEMPTS):
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
                    raise RuntimeError("Contentful request failed after bounded retries.") from None
                time.sleep(2**attempt)
                continue
            status = response.status_code
            if status in (429, 500, 502, 503, 504) and attempt < _ATTEMPTS - 1:
                reset = response.headers.get("X-Contentful-RateLimit-Reset")
                try:
                    delay = float(reset) if reset is not None else float(2**attempt)
                except ValueError:
                    delay = float(2**attempt)
                if not 0 <= delay <= 60:
                    raise RuntimeError("Contentful requested a retry beyond the retry budget.")
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
                if endpoint == "sync" and status == 400 and (params or {}).get("sync_token"):
                    raise RuntimeError(
                        "Contentful rejected the Sync token. Existing memory is preserved; "
                        "rebuild into a fresh dedicated dataset."
                    )
                raise RuntimeError(f"Contentful Delivery request failed (HTTP {status}).")
            try:
                payload = response.json()
            except ValueError:
                raise RuntimeError("Contentful returned invalid JSON.") from None
            if not isinstance(payload, dict):
                raise RuntimeError("Contentful returned an invalid response object.")
            return payload
        raise RuntimeError("Contentful request exhausted its retry budget.")

    def continuation(self, value, endpoint):
        if not isinstance(value, str) or not value:
            raise RuntimeError("Contentful returned an invalid continuation.")
        parsed = urlsplit(urljoin(self.base + endpoint, value))
        try:
            port = parsed.port
        except ValueError:
            raise RuntimeError("Contentful returned an unsafe continuation.") from None
        paths = {
            urlsplit(self.base + endpoint).path,
            self.space_path + endpoint,
        }
        if (
            parsed.scheme != "https"
            or parsed.hostname != self.host
            or port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed.path not in paths
        ):
            raise RuntimeError("Contentful returned an unsafe continuation.")
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        if len({key for key, _ in pairs}) != len(pairs):
            raise RuntimeError("Contentful returned ambiguous continuation parameters.")
        params = dict(pairs)
        if "access_token" in params:
            raise RuntimeError("Contentful returned credentials in a continuation.")
        if endpoint == "sync":
            if set(params) != {"sync_token"} or not params["sync_token"]:
                raise RuntimeError("Contentful returned an invalid Sync continuation.")
            self.secrets.add(params["sync_token"])
        else:
            if (
                not params.get("pageNext")
                or not set(params) <= {"pageNext", "limit", "cursor"}
                or ("cursor" in params and params["cursor"] != "true")
            ):
                raise RuntimeError("Contentful returned an invalid model cursor.")
            if "limit" in params and (
                not params["limit"].isdigit() or not 1 <= int(params["limit"]) <= 1000
            ):
                raise RuntimeError("Contentful returned an invalid model page limit.")
            self.secrets.add(params["pageNext"])
        return params

    def validate_item(self, item, kinds):
        if not isinstance(item, dict) or not isinstance(item.get("sys"), dict):
            raise RuntimeError("Contentful returned invalid resource metadata.")
        system = item["sys"]
        if system.get("type") not in kinds:
            raise RuntimeError("Contentful returned an unsupported resource kind.")
        try:
            identifier(system.get("id"), "resource ID")
        except ValueError:
            raise RuntimeError("Contentful returned an invalid resource ID.") from None
        for key, expected in (("space", self.space), ("environment", self.environment)):
            if key in system:
                link = system[key]
                link_sys = link.get("sys") if isinstance(link, dict) else None
                if not isinstance(link_sys, dict) or link_sys.get("id") != expected:
                    raise RuntimeError(
                        "Contentful resource scope differs from the configured space/environment. "
                        "Use a concrete environment ID, not a retargetable alias."
                    )

    @staticmethod
    def items(payload):
        items = payload.get("items")
        if not isinstance(items, list):
            raise RuntimeError("Contentful returned an invalid collection.")
        return items

    def sync(self, token):
        if token:
            self.secrets.add(token)
        params = {"sync_token": token} if token else {"initial": "true", "limit": 100}
        seen = set()
        events = []
        while True:
            marker = fingerprint(params)
            if marker in seen:
                raise RuntimeError("Contentful repeated a Sync continuation.")
            seen.add(marker)
            payload = self.get("sync", params)
            for item in self.items(payload):
                self.validate_item(item, {"Entry", "Asset", "DeletedEntry", "DeletedAsset"})
                events.append(item)
            page, terminal = payload.get("nextPageUrl"), payload.get("nextSyncUrl")
            if bool(page) == bool(terminal):
                raise RuntimeError("Contentful Sync must return one page or terminal continuation.")
            params = self.continuation(page or terminal, "sync")
            if terminal:
                return events, params["sync_token"]

    def model_inventory(self):
        previous = None
        for _pass in range(3):
            inventory = {}
            params = {"cursor": "true", "limit": 100}
            seen = set()
            while True:
                marker = fingerprint(params)
                if marker in seen:
                    raise RuntimeError("Contentful repeated a model continuation.")
                seen.add(marker)
                payload = self.get("content_types", params)
                if "total" in payload or "skip" in payload:
                    raise RuntimeError("Contentful did not return the requested cursor inventory.")
                for item in self.items(payload):
                    self.validate_item(item, {"ContentType"})
                    key = item["sys"]["id"]
                    if key in inventory:
                        raise RuntimeError("Contentful duplicated a model ID within an inventory.")
                    if not isinstance(item.get("fields"), list):
                        raise RuntimeError("Contentful returned invalid content-model fields.")
                    inventory[key] = item
                pages = payload.get("pages", {})
                if not isinstance(pages, dict):
                    raise RuntimeError("Contentful returned invalid model pagination.")
                if "next" not in pages:
                    break
                params = self.continuation(pages["next"], "content_types")
            hashes = {key: fingerprint(item) for key, item in inventory.items()}
            if previous == hashes:
                return inventory, hashes
            previous = hashes
        raise RuntimeError("Contentful model inventory changed during all three passes.")
