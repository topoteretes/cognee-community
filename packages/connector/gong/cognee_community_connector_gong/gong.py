"""Incremental Gong call documents with full deletion reconciliation."""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

_BATCH_SIZE = 100
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 5


def _utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("Gong dates must include a timezone")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _batches(ids: list[str]) -> Iterator[list[str]]:
    for offset in range(0, len(ids), _BATCH_SIZE):
        yield ids[offset : offset + _BATCH_SIZE]


class GongClient:
    """Read-only Gong Public API client. OAuth tokens and key pairs are both supported."""

    def __init__(
        self,
        base_url: str,
        *,
        access_token: str | None = None,
        access_key: str | None = None,
        access_key_secret: str | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        parsed = urlparse(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.path.strip("/")
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ValueError("GONG_BASE_URL must be an HTTPS origin, without a path")
        if bool(access_token) == bool(access_key and access_key_secret):
            raise ValueError("Provide either an OAuth access token or an access key and secret")
        self.base_url = base_url.rstrip("/")
        self.auth: httpx.Auth | None = (
            httpx.BasicAuth(access_key, access_key_secret) if access_key else None
        )
        self.headers = {"Authorization": f"Bearer {access_token}"} if access_token else {}
        self.http_client = http_client or httpx.Client(timeout=30)
        self._owns_client = http_client is None

    def close(self) -> None:
        if self._owns_client:
            self.http_client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = self.http_client.request(
                    method,
                    f"{self.base_url}{path}",
                    auth=self.auth,
                    headers=self.headers,
                    **kwargs,
                )
            except httpx.TransportError:
                if attempt == _MAX_ATTEMPTS - 1:
                    raise
                time.sleep(min(2**attempt, 30))
                continue
            if response.status_code not in _RETRYABLE_STATUS or attempt == _MAX_ATTEMPTS - 1:
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("Gong returned a non-object JSON response")
                return payload
            retry_after = response.headers.get("Retry-After", "")
            try:
                delay = float(retry_after)
            except ValueError:
                delay = float(2**attempt)
            time.sleep(min(max(delay, 0), 60))
        raise RuntimeError("Gong request retry budget exhausted")

    def _pages(
        self,
        method: str,
        path: str,
        result_key: str,
        *,
        params: dict | None = None,
        body: dict | None = None,
    ) -> Iterator[dict]:
        cursor = None
        seen = 0
        seen_cursors: set[str] = set()
        while True:
            request_params = dict(params or {})
            request_body = dict(body or {})
            if cursor:
                if method == "GET":
                    request_params["cursor"] = cursor
                else:
                    request_body["cursor"] = cursor
            kwargs = {"params": request_params} if method == "GET" else {"json": request_body}
            response = self._request(method, path, **kwargs)
            rows = response.get(result_key)
            records = response.get("records")
            if not isinstance(rows, list) or not isinstance(records, dict):
                raise ValueError(f"Incomplete Gong {path} page; refusing a partial sync")
            page_size = records.get("currentPageSize")
            if isinstance(page_size, int) and page_size != len(rows):
                raise ValueError(f"Incomplete Gong {path} page; refusing a partial sync")
            seen += len(rows)
            yield from rows
            cursor = records.get("cursor")
            if not cursor:
                total = records.get("totalRecords")
                if isinstance(total, int) and seen < total:
                    raise ValueError(f"Incomplete Gong {path} pagination; refusing a partial sync")
                return
            if cursor in seen_cursors:
                raise ValueError(f"Repeated Gong {path} pagination cursor")
            seen_cursors.add(cursor)

    def list_calls(
        self, from_datetime: str, to_datetime: str, workspace_id: str | None = None
    ) -> Iterator[dict]:
        params = {"fromDateTime": from_datetime, "toDateTime": to_datetime}
        if workspace_id:
            params["workspaceId"] = workspace_id
        yield from self._pages("GET", "/v2/calls", "calls", params=params)

    def call_details(self, call_ids: list[str], workspace_id: str | None = None) -> dict[str, dict]:
        details = {}
        for batch in _batches(call_ids):
            filters = {"callIds": batch}
            if workspace_id:
                filters["workspaceId"] = workspace_id
            body = {
                "filter": filters,
                "contentSelector": {"context": "Extended", "exposedFields": {"parties": True}},
            }
            for call in self._pages("POST", "/v2/calls/extensive", "calls", body=body):
                call_id = str((call.get("metaData") or {}).get("id") or "")
                if call_id in batch:
                    details[call_id] = call
        return details

    def transcripts(self, call_ids: list[str], workspace_id: str | None = None) -> dict[str, dict]:
        transcripts = {}
        for batch in _batches(call_ids):
            filters = {"callIds": batch}
            if workspace_id:
                filters["workspaceId"] = workspace_id
            for item in self._pages(
                "POST", "/v2/calls/transcript", "callTranscripts", body={"filter": filters}
            ):
                call_id = str(item.get("callId") or "")
                if call_id in batch:
                    transcripts[call_id] = item
        return transcripts


def _fingerprint(call: dict) -> str:
    """Only metadata that appears in the document can trigger an old-call refresh."""
    fields = [
        call.get(key) for key in ("title", "url", "scheduled", "started", "duration", "direction")
    ]
    return sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def _deal_lines(detail: dict) -> list[str]:
    lines = []
    for context in detail.get("context") or []:
        for obj in context.get("objects") or []:
            if obj.get("objectType") != "Opportunity":
                continue
            fields = {field.get("name"): field.get("value") for field in obj.get("fields") or []}
            name = fields.pop("Name", None) or obj.get("objectId") or "Unknown deal"
            lines.append(f"Deal: {name}")
            for key, value in sorted(fields.items()):
                if key and value is not None:
                    lines.append(f"{key}: {value}")
    return lines


def _call_row(call: dict, detail: dict, transcript: dict) -> dict:
    call_id = str(call["id"])
    title = call.get("title") or f"Gong call {call_id}"
    # Cognee's document adapter adds the title heading from the title column.
    lines = []
    if call.get("started"):
        lines.append(f"Started: {call['started']}")
    if call.get("duration") is not None:
        lines.append(f"Duration: {call['duration']} seconds")
    if call.get("direction"):
        lines.append(f"Direction: {call['direction']}")
    if call.get("url"):
        lines.append(f"Source: {call['url']}")
    lines.extend(_deal_lines(detail))
    speakers = {}
    for party in detail.get("parties") or []:
        if party.get("speakerId") and party.get("name"):
            role = f" ({party['affiliation']})" if party.get("affiliation") else ""
            speakers[str(party["speakerId"])] = f"{party['name']}{role}"
    for monologue in transcript.get("transcript") or []:
        speaker_id = str(monologue.get("speakerId") or "")
        speaker = speakers.get(speaker_id) or (f"Speaker {speaker_id}" if speaker_id else "Speaker")
        for sentence in monologue.get("sentences") or []:
            if sentence.get("text"):
                lines.append(f"{speaker}: {sentence['text']}")
    return {
        "id": call_id,
        "url": call.get("url") or "",
        "title": title,
        "content": "\n".join(lines),
        "_deleted": False,
    }


def sync_calls(
    client: GongClient,
    state: dict,
    *,
    from_datetime: datetime | str,
    workspace_id: str | None = None,
    call_ids: list[str] | None = None,
    include_transcripts: bool = True,
    include_deal_context: bool = True,
    lookback_days: int = 7,
    now: datetime | None = None,
) -> Iterator[dict]:
    """Yield upserts and tombstones; update the dlt cursor only after a complete scan."""
    start = _utc(from_datetime)
    upper = _utc(now or datetime.now(UTC))
    if start >= upper or lookback_days < 0:
        raise ValueError("from_datetime must precede now and lookback_days must be nonnegative")
    selected_ids = set(map(str, call_ids)) if call_ids is not None else None
    scope = [
        getattr(client, "base_url", None),
        _iso(start),
        workspace_id or "",
        sorted(selected_ids) if selected_ids is not None else None,
        include_transcripts,
        include_deal_context,
    ]
    previous: dict[str, str] = state.get("known_calls") or {}
    same_scope = state.get("scope") == scope
    cursor = _utc(state["cursor"]) if same_scope and state.get("cursor") else None

    # Gong has no deleted-call feed. An authoritative inventory is required on
    # every run; a failed page raises before any tombstones or cursor changes.
    inventory = {}
    for call in client.list_calls(_iso(start), _iso(upper), workspace_id):
        call_id = str(call.get("id") or "")
        if not call_id:
            raise ValueError("Gong call without an id; refusing a partial sync")
        if selected_ids is None or call_id in selected_ids:
            inventory[call_id] = call

    candidates = {
        call_id
        for call_id, call in inventory.items()
        if not same_scope or call_id not in previous or _fingerprint(call) != previous[call_id]
    }
    if cursor:
        window_start = max(start, cursor - timedelta(days=lookback_days))
        for call in client.list_calls(_iso(window_start), _iso(upper), workspace_id):
            call_id = str(call.get("id") or "")
            if call_id in inventory:
                candidates.add(call_id)

    upsert_count = 0
    for batch in _batches(sorted(candidates)):
        details = client.call_details(batch, workspace_id) if include_deal_context else {}
        if include_deal_context and set(details) != set(batch):
            raise ValueError("Gong omitted call details; refusing a partial sync")
        transcripts = client.transcripts(batch, workspace_id) if include_transcripts else {}
        for call_id in batch:
            yield _call_row(
                inventory[call_id], details.get(call_id) or {}, transcripts.get(call_id) or {}
            )
            upsert_count += 1

    deleted_ids = set(previous) - set(inventory)
    for call_id in sorted(deleted_ids):
        yield {"id": call_id, "_deleted": True}

    state["known_calls"] = {call_id: _fingerprint(call) for call_id, call in inventory.items()}
    state["scope"] = scope
    state["cursor"] = _iso(upper)
    logger.info("Gong sync: %d upserts, %d deletions", upsert_count, len(deleted_ids))


def gong_source(
    *,
    from_datetime: datetime | str,
    workspace_id: str | None = None,
    call_ids: list[str] | None = None,
    include_transcripts: bool = True,
    include_deal_context: bool = True,
    lookback_days: int = 7,
    base_url: str | None = None,
    access_token: str | None = None,
    access_key: str | None = None,
    access_key_secret: str | None = None,
    client: GongClient | None = None,
):
    """Create a document-mode dlt resource for ``cognee.remember``.

    Pass ``write_disposition='merge'`` to remember so prior calls stay staged.
    Credentials default to ``GONG_*`` environment variables. An OAuth access
    token can be supplied by the caller; refreshing it remains the caller's job.
    """
    try:
        import dlt
        from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR
    except ImportError as exc:
        raise ImportError("Install cognee, dlt and httpx to use the Gong connector") from exc

    @dlt.resource(
        name="gong_calls",
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def gong_calls():
        api = client or GongClient(
            base_url or os.environ.get("GONG_BASE_URL", ""),
            access_token=access_token or os.environ.get("GONG_ACCESS_TOKEN"),
            access_key=access_key or os.environ.get("GONG_ACCESS_KEY"),
            access_key_secret=access_key_secret or os.environ.get("GONG_ACCESS_KEY_SECRET"),
        )
        try:
            yield from sync_calls(
                api,
                dlt.current.resource_state(),
                from_datetime=from_datetime,
                workspace_id=workspace_id,
                call_ids=call_ids,
                include_transcripts=include_transcripts,
                include_deal_context=include_deal_context,
                lookback_days=lookback_days,
            )
        finally:
            if client is None:
                api.close()

    resource = gong_calls()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "gong")
    # The core hashes this with dataset_name, isolating the cursor per dataset.
    setattr(resource, PIPELINE_SCOPE_ATTR, "gong")
    return resource
