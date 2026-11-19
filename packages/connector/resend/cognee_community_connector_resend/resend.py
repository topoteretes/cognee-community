"""Resend email delivery & domain infrastructure data-source connector for cognee.

Synchronizes domains, sent emails, contact audiences, and API key metadata
from Resend into Cognee memory with incremental change detection and forget-on-delete.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Iterable, Sequence
from typing import Any

import dlt
import requests
from dlt.sources import DltResource, DltSource

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.resend.com"
DOCUMENT_SOURCE_ATTR = "resend"
PIPELINE_SCOPE_ATTR = "resend_connector"


class ResendError(Exception):
    """Base exception for Resend API client errors."""


class ResendAuthenticationError(ResendError):
    """Raised when authentication fails (401/403)."""


class ResendNotFoundError(ResendError):
    """Raised when a requested resource is not found (404)."""


class ResendRateLimitError(ResendError):
    """Raised when rate limits are exceeded (429)."""


class ResendServerError(ResendError):
    """Raised when server responds with 5xx status."""


def _validate_api_key(api_key: str) -> None:
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("Resend api_key must be a non-empty string.")
    if any(c in api_key for c in ("\r", "\n", " ")):
        raise ValueError("Resend api_key cannot contain whitespace or newlines.")
    try:
        api_key.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("Resend api_key must contain only ASCII characters.") from exc


def _redact_value(val: Any) -> Any:
    if isinstance(val, str):
        if any(
            tok in val.lower()
            for tok in ("secret", "password", "token", "whsec_", "sk_", "bearer ", "re_")
        ):
            return "[Configured & Protected]"
        if "@" in val and "." in val:
            parts = val.split("@")
            return f"{parts[0][:2]}***@{parts[1]}"
        return val
    if isinstance(val, dict):
        redacted = {}
        for k, v in val.items():
            k_lower = str(k).lower()
            if any(
                tok in k_lower
                for tok in ("secret", "password", "api_key", "token", "credential", "auth")
            ):
                redacted[k] = "[Configured & Protected]"
            else:
                redacted[k] = _redact_value(v)
        return redacted
    if isinstance(val, list):
        return [_redact_value(item) for item in val]
    return val


class ResendClient:
    """Synchronous HTTP client for Resend REST API."""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        session: requests.Session | None = None,
        max_retries: int = 3,
        backoff_factor: float = 0.5,
    ) -> None:
        _validate_api_key(api_key)
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError(f"Invalid base_url: {base_url}. Must start with http:// or https://")

        self.session = session or requests.Session()
        self.max_retries = max(0, max_retries)
        self.backoff_factor = max(0.0, backoff_factor)

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": "cognee-connector-resend/0.1.0",
        }

        retries = 0
        while True:
            try:
                response = self.session.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=params,
                    json=json_data,
                    timeout=30,
                )
            except requests.RequestException as exc:
                if retries < self.max_retries:
                    retries += 1
                    time.sleep(self.backoff_factor * (2 ** (retries - 1)))
                    continue
                raise ResendServerError(f"Connection error connecting to Resend: {exc}") from exc

            status = response.status_code
            if status in (200, 201):
                try:
                    return response.json()
                except Exception as exc:
                    raise ResendServerError("Resend returned invalid JSON response") from exc

            if status in (401, 403):
                raise ResendAuthenticationError(
                    f"Resend authentication failed ({status}): {response.text}"
                )
            if status == 404:
                raise ResendNotFoundError(f"Resend resource not found (404): {url}")
            if status == 429:
                if retries < self.max_retries:
                    retries += 1
                    retry_after = float(
                        response.headers.get(
                            "Retry-After", self.backoff_factor * (2 ** (retries - 1))
                        )
                    )
                    time.sleep(retry_after)
                    continue
                raise ResendRateLimitError("Resend rate limit exceeded (429).")
            if status >= 500:
                if retries < self.max_retries:
                    retries += 1
                    time.sleep(self.backoff_factor * (2 ** (retries - 1)))
                    continue
                raise ResendServerError(f"Resend server error ({status}): {response.text}")

            raise ResendError(f"Resend API error ({status}): {response.text}")

    def list_domains(self) -> dict[str, Any]:
        return self._request("GET", "/domains")

    def get_domain(self, domain_id: str) -> dict[str, Any]:
        return self._request("GET", f"/domains/{domain_id}")

    def list_emails(self, limit: int = 50) -> dict[str, Any]:
        return self._request("GET", "/emails", params={"limit": limit})

    def get_email(self, email_id: str) -> dict[str, Any]:
        return self._request("GET", f"/emails/{email_id}")

    def list_audiences(self) -> dict[str, Any]:
        return self._request("GET", "/audiences")

    def get_audience(self, audience_id: str) -> dict[str, Any]:
        return self._request("GET", f"/audiences/{audience_id}")

    def list_api_keys(self) -> dict[str, Any]:
        return self._request("GET", "/api-keys")


def _format_domain(dom: dict[str, Any]) -> dict[str, Any]:
    did = str(dom.get("id", ""))
    name = dom.get("name") or did
    status = dom.get("status", "unknown")
    region = dom.get("region", "us-east-1")

    content = (
        f"Resend Domain: {name}\n"
        f"Domain ID: {did}\n"
        f"Verification Status: {status}\n"
        f"Region: {region}\n"
        f"Created At: {dom.get('created_at', '')}"
    )

    return {
        "id": f"domain:{did}",
        "title": f"Resend Domain: {name} ({status})",
        "content": content,
        "url": f"https://resend.com/domains/{did}",
        "_deleted": False,
    }


def _format_email(em: dict[str, Any]) -> dict[str, Any]:
    eid = str(em.get("id", ""))
    subject = em.get("subject") or "No Subject"
    from_addr = _redact_value(em.get("from", ""))
    to_addr = _redact_value(em.get("to", ""))
    status = em.get("last_event") or "sent"

    content = (
        f"Resend Email: {eid}\n"
        f"Subject: {subject}\n"
        f"From: {from_addr}\n"
        f"To: {to_addr}\n"
        f"Delivery Status: {status}\n"
        f"Created At: {em.get('created_at', '')}"
    )

    return {
        "id": f"email:{eid}",
        "title": f"Resend Email: {subject} ({status})",
        "content": content,
        "url": f"https://resend.com/emails/{eid}",
        "_deleted": False,
    }


def _format_audience(aud: dict[str, Any]) -> dict[str, Any]:
    aid = str(aud.get("id", ""))
    name = aud.get("name") or aid

    content = (
        f"Resend Audience: {name}\nAudience ID: {aid}\nCreated At: {aud.get('created_at', '')}"
    )

    return {
        "id": f"audience:{aid}",
        "title": f"Resend Audience: {name}",
        "content": content,
        "url": f"https://resend.com/audiences/{aid}",
        "_deleted": False,
    }


def _format_api_key(key: dict[str, Any]) -> dict[str, Any]:
    kid = str(key.get("id", ""))
    name = key.get("name") or kid

    content = (
        f"Resend API Key: {name}\n"
        f"Key ID: {kid}\n"
        f"Token: [Configured & Protected]\n"
        f"Created At: {key.get('created_at', '')}"
    )

    return {
        "id": f"api_key:{kid}",
        "title": f"Resend API Key: {name}",
        "content": content,
        "url": "https://resend.com/api-keys",
        "_deleted": False,
    }


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ResendWalker:
    """Stateful walker traversing Resend resources with incremental hashing and tombstoning."""

    def __init__(self, client: ResendClient, include: Sequence[str]) -> None:
        self.client = client
        self.include = tuple(s.lower() for s in include)

    def walk(self, state: dict[str, Any]) -> Iterable[dict[str, Any]]:
        old_seen_ids: list[str] = state.get("seen_ids", [])
        old_hashes: dict[str, str] = state.get("hashes", {})
        new_seen_ids: list[str] = []
        new_hashes: dict[str, str] = {}

        # 1. Domains
        if "domains" in self.include:
            resp = self.client.list_domains()
            items = resp.get("data") or resp.get("entries") or []
            for item in items:
                doc = _format_domain(item)
                doc_id = doc["id"]
                doc_hash = _sha256(doc["content"])
                new_seen_ids.append(doc_id)
                new_hashes[doc_id] = doc_hash
                if old_hashes.get(doc_id) != doc_hash:
                    yield doc

        # 2. Emails
        if "emails" in self.include:
            resp = self.client.list_emails()
            items = resp.get("data") or resp.get("entries") or []
            for item in items:
                doc = _format_email(item)
                doc_id = doc["id"]
                doc_hash = _sha256(doc["content"])
                new_seen_ids.append(doc_id)
                new_hashes[doc_id] = doc_hash
                if old_hashes.get(doc_id) != doc_hash:
                    yield doc

        # 3. Audiences
        if "audiences" in self.include:
            resp = self.client.list_audiences()
            items = resp.get("data") or resp.get("entries") or []
            for item in items:
                doc = _format_audience(item)
                doc_id = doc["id"]
                doc_hash = _sha256(doc["content"])
                new_seen_ids.append(doc_id)
                new_hashes[doc_id] = doc_hash
                if old_hashes.get(doc_id) != doc_hash:
                    yield doc

        # 4. API Keys
        if "api_keys" in self.include:
            resp = self.client.list_api_keys()
            items = resp.get("data") or resp.get("entries") or []
            for item in items:
                doc = _format_api_key(item)
                doc_id = doc["id"]
                doc_hash = _sha256(doc["content"])
                new_seen_ids.append(doc_id)
                new_hashes[doc_id] = doc_hash
                if old_hashes.get(doc_id) != doc_hash:
                    yield doc

        # Deletion reconciliation / tombstone sweep
        for old_id in old_seen_ids:
            if old_id not in new_seen_ids:
                yield {
                    "id": old_id,
                    "title": f"Deleted entity: {old_id}",
                    "content": f"The resource {old_id} was deleted or removed from Resend.",
                    "url": "https://resend.com/",
                    "_deleted": True,
                }

        state["seen_ids"] = new_seen_ids
        state["hashes"] = new_hashes


def _make_resend_resource(
    api_key: str,
    base_url: str = DEFAULT_BASE_URL,
    include: Sequence[str] = ("domains", "emails", "audiences", "api_keys"),
    client: ResendClient | None = None,
) -> DltResource:
    valid_categories = {"domains", "emails", "audiences", "api_keys"}
    invalid = set(include) - valid_categories
    if invalid:
        raise ValueError(
            f"Invalid categories in include: {invalid}. Must be subset of {valid_categories}"
        )

    active_client = client or ResendClient(api_key=api_key, base_url=base_url)

    @dlt.resource(
        name="resend",
        write_disposition="merge",
        primary_key="id",
        columns={"_deleted": {"data_type": "bool"}},
    )
    def _generator() -> Iterable[dict[str, Any]]:
        state = dlt.current.resource_state()
        walker = ResendWalker(client=active_client, include=include)
        yield from walker.walk(state)

    res = _generator()
    res.DOCUMENT_SOURCE_ATTR = DOCUMENT_SOURCE_ATTR
    res.PIPELINE_SCOPE_ATTR = PIPELINE_SCOPE_ATTR
    return res


def resend_resource(
    api_key: str,
    base_url: str = DEFAULT_BASE_URL,
    include: Sequence[str] = ("domains", "emails", "audiences", "api_keys"),
    client: ResendClient | None = None,
) -> DltResource:
    """Returns a DLT resource for Resend data synchronization."""
    return _make_resend_resource(
        api_key=api_key,
        base_url=base_url,
        include=include,
        client=client,
    )


def resend_source(
    api_key: str,
    base_url: str = DEFAULT_BASE_URL,
    include: Sequence[str] = ("domains", "emails", "audiences", "api_keys"),
    client: ResendClient | None = None,
) -> DltSource:
    """Returns a DLT source encapsulating Resend data synchronization."""

    @dlt.source(name="resend")
    def _source():
        return resend_resource(
            api_key=api_key,
            base_url=base_url,
            include=include,
            client=client,
        )

    return _source()
