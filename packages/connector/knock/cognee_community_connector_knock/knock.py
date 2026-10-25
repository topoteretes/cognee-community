"""Knock notification infrastructure data-source connector for cognee.

Synchronizes workflows, delivery channels, schedules, and message logs from
Knock into Cognee memory with incremental change detection and forget-on-delete.
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

DEFAULT_BASE_URL = "https://api.knock.app/v1"
DOCUMENT_SOURCE_ATTR = "knock"
PIPELINE_SCOPE_ATTR = "knock_connector"


class KnockError(Exception):
    """Base exception for Knock API client errors."""


class KnockAuthenticationError(KnockError):
    """Raised when authentication fails (401/403)."""


class KnockNotFoundError(KnockError):
    """Raised when a requested resource is not found (404)."""


class KnockRateLimitError(KnockError):
    """Raised when rate limits are exceeded (429)."""


class KnockServerError(KnockError):
    """Raised when server responds with 5xx status."""


def _validate_api_key(api_key: str) -> None:
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("Knock api_key must be a non-empty string.")
    if any(c in api_key for c in ("\r", "\n", " ")):
        raise ValueError("Knock api_key cannot contain whitespace or newlines.")
    try:
        api_key.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("Knock api_key must contain only ASCII characters.") from exc


def _redact_value(val: Any) -> Any:
    if isinstance(val, str):
        if any(
            tok in val.lower()
            for tok in ("secret", "password", "token", "whsec_", "sk_", "bearer ")
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
                for tok in ("secret", "password", "api_key", "token", "credential", "auth_header")
            ):
                redacted[k] = "[Configured & Protected]"
            else:
                redacted[k] = _redact_value(v)
        return redacted
    if isinstance(val, list):
        return [_redact_value(item) for item in val]
    return val


class KnockClient:
    """Synchronous HTTP client for Knock REST API v1."""

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
            "User-Agent": "cognee-connector-knock/0.1.0",
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
                raise KnockServerError(f"Connection error connecting to Knock: {exc}") from exc

            status = response.status_code
            if status in (200, 201):
                try:
                    return response.json()
                except Exception as exc:
                    raise KnockServerError("Knock returned invalid JSON response") from exc

            if status in (401, 403):
                raise KnockAuthenticationError(
                    f"Knock authentication failed ({status}): {response.text}"
                )
            if status == 404:
                raise KnockNotFoundError(f"Knock resource not found (404): {url}")
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
                raise KnockRateLimitError("Knock rate limit exceeded (429).")
            if status >= 500:
                if retries < self.max_retries:
                    retries += 1
                    time.sleep(self.backoff_factor * (2 ** (retries - 1)))
                    continue
                raise KnockServerError(f"Knock server error ({status}): {response.text}")

            raise KnockError(f"Knock API error ({status}): {response.text}")

    def list_workflows(self, page_size: int = 50, after: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"page_size": page_size}
        if after:
            params["after"] = after
        return self._request("GET", "/workflows", params=params)

    def get_workflow(self, key: str) -> dict[str, Any]:
        return self._request("GET", f"/workflows/{key}")

    def list_channels(self, page_size: int = 50, after: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"page_size": page_size}
        if after:
            params["after"] = after
        return self._request("GET", "/channels", params=params)

    def get_channel(self, channel_id: str) -> dict[str, Any]:
        return self._request("GET", f"/channels/{channel_id}")

    def list_schedules(self, page_size: int = 50, after: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"page_size": page_size}
        if after:
            params["after"] = after
        return self._request("GET", "/schedules", params=params)

    def get_schedule(self, schedule_id: str) -> dict[str, Any]:
        return self._request("GET", f"/schedules/{schedule_id}")

    def list_messages(self, page_size: int = 50, after: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"page_size": page_size}
        if after:
            params["after"] = after
        return self._request("GET", "/messages", params=params)

    def get_message(self, message_id: str) -> dict[str, Any]:
        return self._request("GET", f"/messages/{message_id}")


def _format_workflow(wf: dict[str, Any]) -> dict[str, Any]:
    key = str(wf.get("key", ""))
    name = wf.get("name") or key
    desc = wf.get("description") or "No description provided."
    steps = wf.get("steps") or []
    step_summaries = [
        f"- Step {i + 1}: {s.get('type')} ({s.get('channel_id', 'no-channel')})"
        for i, s in enumerate(steps)
    ]
    steps_block = "\n".join(step_summaries) if step_summaries else "No steps configured."

    content = (
        f"Knock Workflow: {name}\n"
        f"Key: {key}\n"
        f"Description: {desc}\n"
        f"Active: {wf.get('active', True)}\n"
        f"Steps:\n{steps_block}\n"
        f"Updated At: {wf.get('updated_at', '')}"
    )

    return {
        "id": f"workflow:{key}",
        "title": f"Knock Workflow: {name}",
        "content": content,
        "url": f"https://dashboard.knock.app/workflows/{key}",
        "_deleted": False,
    }


def _format_channel(ch: dict[str, Any]) -> dict[str, Any]:
    cid = str(ch.get("id", ""))
    name = ch.get("name") or cid
    ctype = ch.get("type", "unknown")
    provider = ch.get("provider", "unknown")

    content = (
        f"Knock Delivery Channel: {name}\n"
        f"Channel ID: {cid}\n"
        f"Channel Type: {ctype}\n"
        f"Provider: {provider}\n"
        f"Created At: {ch.get('created_at', '')}\n"
        f"Updated At: {ch.get('updated_at', '')}"
    )

    return {
        "id": f"channel:{cid}",
        "title": f"Knock Channel: {name} ({ctype})",
        "content": content,
        "url": f"https://dashboard.knock.app/integrations/channels/{cid}",
        "_deleted": False,
    }


def _format_schedule(sch: dict[str, Any]) -> dict[str, Any]:
    sid = str(sch.get("id", ""))
    workflow = sch.get("workflow", "")
    recipient = _redact_value(sch.get("recipient", "unknown"))
    repeats = sch.get("repeats", "unknown")

    content = (
        f"Knock Schedule: {sid}\n"
        f"Target Workflow: {workflow}\n"
        f"Recipient: {recipient}\n"
        f"Repeats: {repeats}\n"
        f"Next Occurrence: {sch.get('next_occurrence_at', '')}\n"
        f"Ending At: {sch.get('ending_at', 'never')}"
    )

    return {
        "id": f"schedule:{sid}",
        "title": f"Knock Schedule: {sid} ({workflow})",
        "content": content,
        "url": f"https://dashboard.knock.app/schedules/{sid}",
        "_deleted": False,
    }


def _format_message(msg: dict[str, Any]) -> dict[str, Any]:
    mid = str(msg.get("id", ""))
    workflow = msg.get("workflow", "")
    status = msg.get("status", "unknown")
    channel_id = msg.get("channel_id", "")
    recipient = _redact_value(msg.get("recipient", "unknown"))

    content = (
        f"Knock Message: {mid}\n"
        f"Workflow: {workflow}\n"
        f"Status: {status}\n"
        f"Channel ID: {channel_id}\n"
        f"Recipient: {recipient}\n"
        f"Created At: {msg.get('created_at', '')}\n"
        f"Updated At: {msg.get('updated_at', '')}"
    )

    return {
        "id": f"message:{mid}",
        "title": f"Knock Message: {mid} ({status})",
        "content": content,
        "url": f"https://dashboard.knock.app/messages/{mid}",
        "_deleted": False,
    }


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class KnockWalker:
    """Stateful walker traversing Knock resources with incremental hashing and tombstoning."""

    def __init__(self, client: KnockClient, include: Sequence[str]) -> None:
        self.client = client
        self.include = tuple(s.lower() for s in include)

    def walk(self, state: dict[str, Any]) -> Iterable[dict[str, Any]]:
        old_seen_ids: list[str] = state.get("seen_ids", [])
        old_hashes: dict[str, str] = state.get("hashes", {})
        new_seen_ids: list[str] = []
        new_hashes: dict[str, str] = {}

        # 1. Workflows
        if "workflows" in self.include:
            after = None
            while True:
                resp = self.client.list_workflows(page_size=50, after=after)
                items = resp.get("entries") or resp.get("data") or []
                for item in items:
                    doc = _format_workflow(item)
                    doc_id = doc["id"]
                    doc_hash = _sha256(doc["content"])
                    new_seen_ids.append(doc_id)
                    new_hashes[doc_id] = doc_hash
                    if old_hashes.get(doc_id) != doc_hash:
                        yield doc
                page_info = resp.get("page_info", {})
                after = page_info.get("after")
                if not after or not items:
                    break

        # 2. Channels
        if "channels" in self.include:
            after = None
            while True:
                resp = self.client.list_channels(page_size=50, after=after)
                items = resp.get("entries") or resp.get("data") or []
                for item in items:
                    doc = _format_channel(item)
                    doc_id = doc["id"]
                    doc_hash = _sha256(doc["content"])
                    new_seen_ids.append(doc_id)
                    new_hashes[doc_id] = doc_hash
                    if old_hashes.get(doc_id) != doc_hash:
                        yield doc
                page_info = resp.get("page_info", {})
                after = page_info.get("after")
                if not after or not items:
                    break

        # 3. Schedules
        if "schedules" in self.include:
            after = None
            while True:
                resp = self.client.list_schedules(page_size=50, after=after)
                items = resp.get("entries") or resp.get("data") or []
                for item in items:
                    doc = _format_schedule(item)
                    doc_id = doc["id"]
                    doc_hash = _sha256(doc["content"])
                    new_seen_ids.append(doc_id)
                    new_hashes[doc_id] = doc_hash
                    if old_hashes.get(doc_id) != doc_hash:
                        yield doc
                page_info = resp.get("page_info", {})
                after = page_info.get("after")
                if not after or not items:
                    break

        # 4. Messages
        if "messages" in self.include:
            after = None
            while True:
                resp = self.client.list_messages(page_size=50, after=after)
                items = resp.get("entries") or resp.get("data") or []
                for item in items:
                    doc = _format_message(item)
                    doc_id = doc["id"]
                    doc_hash = _sha256(doc["content"])
                    new_seen_ids.append(doc_id)
                    new_hashes[doc_id] = doc_hash
                    if old_hashes.get(doc_id) != doc_hash:
                        yield doc
                page_info = resp.get("page_info", {})
                after = page_info.get("after")
                if not after or not items:
                    break

        # Deletion reconciliation / tombstone sweep
        for old_id in old_seen_ids:
            if old_id not in new_seen_ids:
                yield {
                    "id": old_id,
                    "title": f"Deleted entity: {old_id}",
                    "content": f"The resource {old_id} was deleted or removed from Knock.",
                    "url": "https://dashboard.knock.app/",
                    "_deleted": True,
                }

        state["seen_ids"] = new_seen_ids
        state["hashes"] = new_hashes


def _make_knock_resource(
    api_key: str,
    base_url: str = DEFAULT_BASE_URL,
    include: Sequence[str] = ("workflows", "channels", "schedules", "messages"),
    client: KnockClient | None = None,
) -> DltResource:
    valid_categories = {"workflows", "channels", "schedules", "messages"}
    invalid = set(include) - valid_categories
    if invalid:
        raise ValueError(
            f"Invalid categories in include: {invalid}. Must be subset of {valid_categories}"
        )

    active_client = client or KnockClient(api_key=api_key, base_url=base_url)

    @dlt.resource(
        name="knock",
        write_disposition="merge",
        primary_key="id",
        columns={"_deleted": {"data_type": "bool"}},
    )
    def _generator() -> Iterable[dict[str, Any]]:
        state = dlt.current.resource_state()
        walker = KnockWalker(client=active_client, include=include)
        yield from walker.walk(state)

    res = _generator()
    res.DOCUMENT_SOURCE_ATTR = DOCUMENT_SOURCE_ATTR
    res.PIPELINE_SCOPE_ATTR = PIPELINE_SCOPE_ATTR
    return res


def knock_resource(
    api_key: str,
    base_url: str = DEFAULT_BASE_URL,
    include: Sequence[str] = ("workflows", "channels", "schedules", "messages"),
    client: KnockClient | None = None,
) -> DltResource:
    """Returns a DLT resource for Knock data synchronization."""
    return _make_knock_resource(
        api_key=api_key,
        base_url=base_url,
        include=include,
        client=client,
    )


def knock_source(
    api_key: str,
    base_url: str = DEFAULT_BASE_URL,
    include: Sequence[str] = ("workflows", "channels", "schedules", "messages"),
    client: KnockClient | None = None,
) -> DltSource:
    """Returns a DLT source encapsulating Knock data synchronization."""

    @dlt.source(name="knock")
    def _source():
        return knock_resource(
            api_key=api_key,
            base_url=base_url,
            include=include,
            client=client,
        )

    return _source()
