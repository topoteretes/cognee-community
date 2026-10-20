"""Read-only REST evidence for session export and deletion discovery.

Audit pagination is documented; record-level deletion semantics are not. Audit
records never become Cognee documents or tombstones. Export files are not fetched.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

import httpx2

from cognee_community_connector_logrocket.credentials import CredentialError, resolve_api_key


class LogRocketRESTError(RuntimeError):
    """A failure that never includes credentials, URLs, cursors or response data."""

    def __init__(self, code: str, *, status: int | None = None):
        self.code = code
        self.status = status
        suffix = f" (HTTP {status})" if status is not None else ""
        super().__init__(f"LogRocket REST {code}{suffix}; response details withheld.")


def _cursor(value: Any) -> str:
    # The docs initially show numeric cursors but explicitly require treating
    # continuations as opaque strings, including same-millisecond tie breakers.
    if isinstance(value, str) and value:
        return value
    if type(value) is int and value >= 0:
        return str(value)
    raise LogRocketRESTError("invalid_cursor")


def _positive_int(value: Any, maximum: int, name: str) -> None:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 1 and {maximum}.")


@dataclass(frozen=True)
class AuditPage:
    records: tuple[dict[str, Any], ...] = field(repr=False)
    has_next: bool
    cursor: str = field(repr=False)


@dataclass(frozen=True)
class AuditBatch:
    """A completely read bounded audit traversal, not a session inventory."""

    records: tuple[dict[str, Any], ...] = field(repr=False)
    cursor: str = field(repr=False)
    pages: int


def parse_audit_page(payload: dict[str, Any], *, limit: int) -> AuditPage:
    if not isinstance(payload, dict):
        raise LogRocketRESTError("invalid_audit_page")
    records = payload.get("logs")
    if not isinstance(records, list) or len(records) > limit:
        raise LogRocketRESTError("invalid_audit_records")
    if type(payload.get("has_next")) is not bool:
        raise LogRocketRESTError("invalid_audit_continuation")
    # Observed live on 2026-10-19: an empty terminal page returns cursor="".
    # This is not a continuation and must never replace the caller's position.
    cursor = (
        ""
        if not records and payload["has_next"] is False and payload.get("cursor") == ""
        else _cursor(payload.get("cursor"))
    )
    for row in records:
        if (
            not isinstance(row, dict)
            or any(
                not isinstance(row.get(key), str)
                for key in ("created_date", "user", "action", "description")
            )
            or ("time" in row and not isinstance(row["time"], str))
        ):
            raise LogRocketRESTError("invalid_audit_record")
    return AuditPage(tuple(records), payload["has_next"], cursor)


class LogRocketRESTClient:
    """Project-scoped GET-only client for documented audit/export endpoints."""

    def __init__(
        self,
        organization_id: str,
        project_id: str,
        *,
        api_key: str | None = None,
        api_key_env: str = "LOGROCKET_API_KEY",
        base_url: str = "https://api.logrocket.com",
        transport: httpx2.AsyncBaseTransport | None = None,
    ):
        if not organization_id.strip() or not project_id.strip():
            raise ValueError("organization_id and project_id are required.")
        if organization_id.strip() in {".", ".."} or project_id.strip() in {".", ".."}:
            raise ValueError("organization_id and project_id cannot be path traversal segments.")
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("base_url must be an HTTPS origin without credentials, path or query.")
        self._base = (
            f"{base_url.rstrip('/')}/v1/orgs/{quote(organization_id.strip(), safe='')}"
            f"/apps/{quote(project_id.strip(), safe='')}/"
        )
        self._api_key = api_key
        self._api_key_env = api_key_env
        self._transport = transport

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            api_key = resolve_api_key(self._api_key, self._api_key_env)
        except CredentialError as exc:
            raise LogRocketRESTError(exc.code) from None
        try:
            async with asyncio.timeout(60):
                async with httpx2.AsyncClient(
                    headers={"Authorization": f"token {api_key}"},
                    timeout=15,
                    follow_redirects=False,
                    transport=self._transport,
                ) as client:
                    for attempt in range(3):
                        try:
                            response = await client.get(self._base + path, params=params)
                        except (httpx2.TransportError, TimeoutError):
                            if attempt == 2:
                                raise LogRocketRESTError("transport_failure") from None
                        else:
                            status = response.status_code
                            if status in {408, 429, 500, 502, 503, 504}:
                                if attempt == 2:
                                    raise LogRocketRESTError(
                                        "transient_http_failure", status=status
                                    )
                            elif not 200 <= status < 300:
                                raise LogRocketRESTError("http_failure", status=status)
                            else:
                                try:
                                    payload = response.json()
                                except (ValueError, TypeError):
                                    raise LogRocketRESTError(
                                        "invalid_json", status=status
                                    ) from None
                                if not isinstance(payload, dict):
                                    raise LogRocketRESTError("invalid_json_object", status=status)
                                return payload
                        await asyncio.sleep(2**attempt)
        except LogRocketRESTError:
            raise
        except TimeoutError:
            raise LogRocketRESTError("timeout") from None
        except Exception:
            raise LogRocketRESTError("request_failure") from None
        raise LogRocketRESTError("request_failure")  # pragma: no cover

    async def audit_page(self, *, cursor: str, limit: int = 100) -> AuditPage:
        _positive_int(limit, 100, "limit")
        payload = await self._get("audit/logs/", {"cursor": _cursor(cursor), "limit": limit})
        return parse_audit_page(payload, limit=limit)

    async def read_audit_logs(
        self,
        *,
        cursor: str,
        limit: int = 100,
        max_pages: int = 100,
    ) -> AuditBatch:
        """Follow has_next to completion or fail, without committing caller state.

        This reads audit activity, including potentially sensitive employee data.
        A batch does not establish deletion IDs, completion, or inventory absence.
        """
        _positive_int(limit, 100, "limit")
        _positive_int(max_pages, 100, "max_pages")
        cursor = _cursor(cursor)
        seen = {cursor}
        records: list[dict[str, Any]] = []
        try:
            async with asyncio.timeout(120):
                for page_number in range(1, max_pages + 1):
                    page = await self.audit_page(cursor=cursor, limit=limit)
                    # A terminal page may legitimately echo the last cursor.
                    if page.has_next and (page.cursor in seen or not page.records):
                        raise LogRocketRESTError("audit_cursor_not_progressing")
                    records.extend(page.records)
                    if not page.has_next:
                        return AuditBatch(tuple(records), page.cursor or cursor, page_number)
                    cursor = page.cursor
                    seen.add(cursor)
        except TimeoutError:
            raise LogRocketRESTError("audit_timeout") from None
        raise LogRocketRESTError("audit_page_budget_exceeded")

    async def export_page(
        self,
        *,
        cursor: str | None = None,
        since_ms: int | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        """Read one legacy export listing; its end-of-stream contract is unverified.

        No export URLs are followed, and this never authorizes replacement or
        cursor advancement in the ingestion source.
        """
        _positive_int(limit, 100, "limit")
        if cursor is not None and since_ms is not None:
            raise ValueError("Use either cursor or since_ms, not both.")
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = _cursor(cursor)
        if since_ms is not None:
            if type(since_ms) is not int or since_ms < 0:
                raise ValueError("since_ms must be a non-negative integer timestamp.")
            params["date"] = since_ms
        payload = await self._get("data-export/", params)
        records = payload.get("sessions")
        if not isinstance(records, list) or len(records) > limit:
            raise LogRocketRESTError("invalid_export_records")
        _cursor(payload.get("cursor"))
        if any(
            not isinstance(row, dict) or not isinstance(row.get("url"), str) or not row["url"]
            for row in records
        ):
            raise LogRocketRESTError("invalid_export_record")
        return payload

    async def probe(
        self,
        resource: str,
        *,
        cursor: str | None = None,
        since_ms: int | None = None,
        limit: int = 2,
        max_pages: int = 1,
    ):
        """Return counts and field types, never record values or opaque cursors."""
        from cognee_community_connector_logrocket.probe import response_shape

        _positive_int(limit, 5, "probe limit")
        _positive_int(max_pages, 5, "probe max_pages")
        if resource not in {"audit", "session-exports"}:
            raise ValueError("resource must be audit or session-exports.")
        if cursor is not None and since_ms is not None:
            raise ValueError("Use either cursor or since_ms, not both.")
        if since_ms is not None and (type(since_ms) is not int or since_ms < 0):
            raise ValueError("since_ms must be a non-negative integer timestamp.")
        if resource == "audit" and cursor is None and since_ms is None:
            raise ValueError("Audit inspection requires cursor or since_ms.")
        report: dict[str, Any] = {
            "resource": resource,
            "automatic_deletion_supported": False,
            "session_inventory_complete": False,
            "issue_inventory_complete": False,
        }
        try:
            if resource == "audit":
                initial = cursor if cursor is not None else str(since_ms)
                batch = await self.read_audit_logs(cursor=initial, limit=limit, max_pages=max_pages)
                known = {"time", "created_date", "user", "action", "description"}
                report.update(
                    {
                        "status": "ok",
                        "pages": batch.pages,
                        "record_count": len(batch.records),
                        "audit_traversal_complete": True,
                        "records_shape": response_shape(
                            [
                                {key: value for key, value in row.items() if key in known}
                                for row in batch.records
                            ]
                        ),
                        "additional_record_fields_present": any(
                            set(row) - known for row in batch.records
                        ),
                        "deletion_contract": "unverified_even_when_audit_traversal_is_complete",
                    }
                )
            else:
                payload = await self.export_page(cursor=cursor, since_ms=since_ms, limit=limit)
                report.update(
                    {
                        "status": "ok",
                        "pages": 1,
                        "record_count": len(payload["sessions"]),
                        "records_shape": response_shape(
                            [{"url": row["url"]} for row in payload["sessions"]]
                        ),
                        "export_completion_contract": "unverified",
                    }
                )
        except LogRocketRESTError as exc:
            report.update({"status": "error", "error_code": exc.code, "http_status": exc.status})
        return report
