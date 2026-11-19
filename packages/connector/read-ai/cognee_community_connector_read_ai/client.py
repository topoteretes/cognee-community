"""Bounded Read.ai REST reconciliation; absence is corroborated within this run."""

from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx
from filelock import FileLock

from .auth import OAuthIdentity, verify_identity
from .models import (
    AuthenticationError,
    BudgetExceededError,
    ConfigurationError,
    IncompleteSyncError,
    Selection,
    Snapshot,
    SyncLimits,
)
from .render import meeting_id, normalize_meeting, render_meeting

API_BASE = "https://api.read.ai/v1/meetings"


class ReadAIClient:
    def __init__(
        self,
        token_provider,
        *,
        client: httpx.Client | None = None,
        limits: SyncLimits,
        rate_dir: Path,
    ):
        self.token_provider = token_provider
        self.client = client or httpx.Client(follow_redirects=False)
        self._owned_client = client is None
        self.limits = limits
        self.rate_dir = Path(rate_dir)
        self.deadline = math.inf
        self._bytes = 0
        self._pages = 0
        self._seen_ids: set[str] = set()
        self._rate_key = "unbound"

    def close(self):
        if self._owned_client:
            self.client.close()

    def check_budget(self) -> None:
        """Check the same run deadline while foreground storage work completes."""
        self._remaining()

    def _remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise BudgetExceededError("The Read.ai synchronization deadline was exceeded")
        return remaining

    def _sleep(self, seconds: float):
        if seconds >= self._remaining():
            raise BudgetExceededError("A Read.ai retry or rate-limit wait exceeds the deadline")
        time.sleep(seconds)
        self._remaining()

    def _reserve_request(self):
        """Serialize an account-wide sliding window, shared between processes."""
        self.rate_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.rate_dir / f"{self._rate_key}.json"
        lock = FileLock(str(path) + ".lock", timeout=self._remaining())
        while True:
            with lock:
                now = time.time()
                try:
                    stamps = json.loads(path.read_text()) if path.exists() else []
                except (OSError, ValueError) as error:
                    raise IncompleteSyncError("Read.ai shared rate state is unreadable") from error
                if not isinstance(stamps, list) or any(
                    isinstance(stamp, bool)
                    or not isinstance(stamp, (float, int))
                    or not math.isfinite(stamp)
                    for stamp in stamps
                ):
                    raise IncompleteSyncError("Read.ai shared rate state is invalid")
                # Future stamps remain reserved after a backwards clock adjustment.
                stamps = sorted(stamp for stamp in stamps if stamp > now - 60)
                if len(stamps) < 90:
                    stamps.append(now)
                    temporary = path.with_suffix(".tmp")
                    temporary.write_text(json.dumps(stamps))
                    temporary.chmod(0o600)
                    temporary.replace(path)
                    return
                delay = max(0.01, stamps[0] + 60 - now)
            self._sleep(delay)
            lock.timeout = self._remaining()

    @staticmethod
    def _retry_after(value: str | None, attempt: int) -> float:
        if value:
            try:
                delay = float(value)
            except ValueError:
                try:
                    parsed = parsedate_to_datetime(value)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=UTC)
                    delay = parsed.timestamp() - time.time()
                except (ValueError, TypeError, OverflowError):
                    delay = math.nan
            if math.isfinite(delay) and delay >= 0:
                if delay > 300:
                    raise IncompleteSyncError("Read.ai requested a retry delay above 300 seconds")
                return delay
        return min(2**attempt, 16)

    def _request_json(
        self,
        url: str,
        *,
        params: Any = None,
        headers: dict | None = None,
        authenticated: bool = False,
        missing_ok: bool = False,
    ) -> dict | None:
        refreshed = False
        for attempt in range(5):
            self._reserve_request()
            request_headers = {
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                **(headers or {}),
            }
            if authenticated:
                tokens = self.token_provider.get_tokens()
                request_headers["Authorization"] = f"Bearer {tokens.access_token}"
            try:
                with self.client.stream(
                    "GET",
                    url,
                    params=params,
                    headers=request_headers,
                    follow_redirects=False,
                    timeout=min(30.0, self._remaining()),
                ) as response:
                    # Reject unexpected compression before invoking HTTPX's decoder:
                    # one compressed chunk could otherwise allocate beyond the cap.
                    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                        raise IncompleteSyncError("Read.ai returned unexpected compressed content")
                    body = bytearray()
                    for chunk in response.iter_bytes(chunk_size=65536):
                        self._remaining()
                        body.extend(chunk)
                        self._bytes += len(chunk)
                        if len(body) > self.limits.max_response_bytes:
                            raise BudgetExceededError("Read.ai exceeded the decoded response limit")
                        if self._bytes > self.limits.max_total_response_bytes:
                            raise BudgetExceededError("Read.ai exceeded the total response limit")
                    status = response.status_code
                    retry_after = response.headers.get("Retry-After")
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as error:
                if attempt == 4:
                    raise IncompleteSyncError("Read.ai transport retries were exhausted") from error
                self._sleep(self._retry_after(None, attempt))
                continue
            if status == 401 and authenticated and not refreshed:
                self.token_provider.get_tokens(force_refresh=True)
                refreshed = True
                continue
            if status in (401, 403):
                raise AuthenticationError(
                    f"Read.ai refused access (HTTP {status}); sync is incomplete"
                )
            if status == 404 and missing_ok:
                return None
            if status == 429 or 500 <= status <= 599:
                if attempt == 4:
                    raise IncompleteSyncError(f"Read.ai HTTP {status} retries were exhausted")
                self._sleep(self._retry_after(retry_after, attempt))
                continue
            if status != 200:
                raise IncompleteSyncError(f"Read.ai returned HTTP {status}; sync is incomplete")
            try:
                value = json.loads(body)
            except (ValueError, UnicodeError, RecursionError) as error:
                raise IncompleteSyncError("Read.ai returned malformed JSON") from error
            if not isinstance(value, dict):
                raise IncompleteSyncError("Read.ai returned a non-object response")
            return value
        raise IncompleteSyncError("Read.ai request retries were exhausted")

    def _count_id(self, identity: str):
        self._seen_ids.add(identity)
        if len(self._seen_ids) > self.limits.max_meetings:
            raise BudgetExceededError("Read.ai exceeded the distinct meeting limit")

    def _inventory(self, selection: Selection, upper: int) -> dict[str, dict]:
        params: dict[str, Any] = {"limit": 10, "start_time_ms.lt": upper}
        if selection.start_time_ms is not None:
            params["start_time_ms.gte"] = selection.start_time_ms
        meetings = {}
        cursors = set()
        # Context-only normalization also validates all list meeting metadata.
        context = Selection(include_summary=True, include_transcript=False, include_metrics=False)
        while True:
            self._pages += 1
            if self._pages > self.limits.max_pages:
                raise BudgetExceededError("Read.ai exceeded the inventory page limit")
            page = self._request_json(API_BASE, params=params, authenticated=True)
            if (
                page.get("object") != "list"
                or not isinstance(page.get("has_more"), bool)
                or not isinstance(page.get("data"), list)
                or len(page["data"]) > 10
            ):
                raise IncompleteSyncError("Read.ai returned an invalid inventory page")
            for raw in page["data"]:
                normalized = normalize_meeting(raw, context)
                identity = normalized["id"]
                self._count_id(identity)
                stamp = normalized["start_time_ms"]
                if stamp >= upper or (
                    selection.start_time_ms is not None and stamp < selection.start_time_ms
                ):
                    raise IncompleteSyncError(
                        "Read.ai inventory violates the requested date bounds"
                    )
                if identity in meetings:
                    raise IncompleteSyncError("Read.ai inventory repeated a meeting ID")
                meetings[identity] = raw
            if not page["has_more"]:
                return meetings
            if not page["data"]:
                raise IncompleteSyncError("Read.ai returned an empty nonterminal inventory page")
            cursor = page["data"][-1]["id"]
            if cursor in cursors:
                raise IncompleteSyncError("Read.ai inventory repeated a pagination cursor")
            cursors.add(cursor)
            params["cursor"] = cursor

    def _detail(self, identity: str, selection: Selection) -> dict | None:
        meeting_id(identity)
        self._count_id(identity)
        params = [
            ("expand[]", section)
            for section in ("summary", "transcript", "metrics")
            if getattr(selection, f"include_{section}")
        ]
        raw = self._request_json(
            f"{API_BASE}/{identity}", params=params, authenticated=True, missing_ok=True
        )
        if raw is not None:
            normalized = normalize_meeting(raw, selection)
            if normalized["id"] != identity:
                raise IncompleteSyncError("Read.ai returned another meeting's detail")
            return normalized
        return None

    def reconcile(
        self,
        selection: Selection,
        previous_ids: set[str],
        expected_identity: OAuthIdentity | None = None,
        *,
        deadline: float | None = None,
    ) -> Snapshot:
        self.deadline = time.monotonic() + self.limits.max_duration_seconds
        if deadline is not None:
            self.deadline = min(self.deadline, deadline)
        self._remaining()
        self._bytes = self._pages = 0
        self._seen_ids = set()
        bundle = self.token_provider.get_tokens()
        if expected_identity is None:
            initial_key = bundle.client_id
        else:
            initial_key = f"{expected_identity.issuer}\0{expected_identity.subject}"
        self._rate_key = hashlib.sha256(initial_key.encode()).hexdigest()
        identity = verify_identity(
            bundle,
            client=self.client,
            expected=expected_identity,
            request_json=self._request_json,
        )
        self._rate_key = hashlib.sha256(
            f"{identity.issuer}\0{identity.subject}".encode()
        ).hexdigest()
        for previous in previous_ids:
            meeting_id(previous)
            self._count_id(previous)
        meetings = {}
        if selection.meeting_ids is not None:
            if len(set(selection.meeting_ids)) != len(selection.meeting_ids):
                raise ConfigurationError("Explicit meeting IDs must be distinct")
            for selected in selection.meeting_ids:
                meeting = self._detail(selected, selection)
                if meeting is None:
                    if self._detail(selected, selection) is not None:
                        raise IncompleteSyncError("Read.ai absence changed during confirmation")
                else:
                    meetings[selected] = meeting
        else:
            upper = selection.end_time_ms
            if upper is None:
                upper = int(datetime.now(UTC).timestamp() * 1000)
            inventory = self._inventory(selection, upper)
            for selected, raw in inventory.items():
                if raw.get("end_time_ms") is None:
                    meetings[selected] = normalize_meeting(raw, selection)
                    continue
                meeting = self._detail(selected, selection)
                if meeting is None:
                    raise IncompleteSyncError("A listed Read.ai meeting vanished during extraction")
                if meeting["start_time_ms"] != raw["start_time_ms"]:
                    raise IncompleteSyncError("Read.ai meeting changed its selected start time")
                meetings[selected] = meeting
            missing = previous_ids - set(inventory)
            for absent in sorted(missing):
                if self._detail(absent, selection) is not None:
                    raise IncompleteSyncError("A missing Read.ai report is still retrievable")
            if missing:
                confirmation = self._inventory(selection, upper)
                # Any membership change requires a fresh complete attempt.
                if set(confirmation) != set(inventory):
                    raise IncompleteSyncError(
                        "Read.ai inventory changed during removal confirmation"
                    )
        rows = {}
        pending = []
        for selected, meeting in sorted(meetings.items()):
            row = render_meeting(meeting)
            if row is not None:
                rows[selected] = row
            if meeting["active"] or any(
                state == "unavailable" for state in meeting["availability"].values()
            ):
                pending.append(selected)
        self._remaining()
        return Snapshot(meetings=meetings, rows=rows, pending=pending, identity=identity)
