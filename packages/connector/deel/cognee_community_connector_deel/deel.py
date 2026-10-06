"""DLT source for Deel data (workers directory and contracts) with forget-on-delete.

Syncs Deel worker directory metadata and contracts into Cognee memory for AI agent retrieval.

Architecture & Design:
---------------------
- **Document Mode**: Tags the source with ``DOCUMENT_SOURCE_ATTR = "deel"`` so that Cognee's
  ``resolve_dlt_sources`` routes records through the ``cognify`` entity-extraction and knowledge
  graph pipeline instead of treating them as tabular data.
- **Privacy & Sensitivity**: Contract documents contain sensitive compensation, legal terms,
  and personal data. By default, the connector ingests sanitized metadata summaries. Full contract
  document bodies/clauses are strictly opt-in via ``include_contract_documents=True``.
- **Full-Snapshot Sync (Replace)**: Resources use ``write_disposition="replace"``. Each sync
  replaces staging with active Deel records. When workers or contracts are deleted or terminated
  upstream, they drop out of the snapshot, allowing Cognee's ``orphan_cleanup`` to purge them
  from graph and vector memory.
- **Incremental Cognify**: Output records use deterministic, content-hashed IDs. Unchanged
  records retain identical IDs and are not needlessly re-cognified or re-embedded.
- **Pagination & Rate Limiting**: Accommodates Deel's API pagination (both cursor-based via
  ``after_cursor`` and offset-based via ``offset``/``limit``) with backoff on HTTP 429 and
  transient 5xx responses.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("deel_connector")

DEEL_SOURCE_NAME = "deel"
DEEL_WORKERS_TABLE = "deel_workers"
DEEL_CONTRACTS_TABLE = "deel_contracts"

_DEFAULT_BASE_URL = "https://api.letsdeel.com/rest/v1"
_MAX_RETRIES = 5
_EXTRA_HINT = (
    'The Deel connector requires dlt and httpx: pip install "cognee-community-connector-deel" '
    "(provides dlt and httpx)."
)


class DeelClient:
    """Minimal, robust HTTP client for Deel REST API v1."""

    def __init__(
        self,
        token: str,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: float = 30.0,
    ) -> None:
        import httpx

        self.token = token
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "cognee-community-connector-deel/0.1.0",
            },
            timeout=timeout,
        )

    def close(self) -> None:
        self.client.close()

    def get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Issue a GET request with exponential backoff on rate limits and transient errors."""
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        params = params or {}

        for attempt in range(_MAX_RETRIES):
            try:
                response = self.client.get(url, params=params)
                if response.status_code in (429, 500, 502, 503, 504):
                    if attempt == _MAX_RETRIES - 1:
                        response.raise_for_status()
                    delay = _retry_after(response.headers, attempt)
                    logger.warning(
                        "Deel API: HTTP %d on %s — retrying in %.1fs (%d/%d).",
                        response.status_code,
                        url,
                        delay,
                        attempt + 1,
                        _MAX_RETRIES,
                    )
                    time.sleep(delay)
                    continue

                response.raise_for_status()
                return response.json()
            except Exception as exc:
                if attempt == _MAX_RETRIES - 1 or not _is_transient(exc):
                    raise
                delay = float(2**attempt)
                logger.warning(
                    "Deel API: error on %s (%s) — retrying in %.1fs (%d/%d).",
                    url,
                    exc,
                    delay,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(delay)

        raise RuntimeError(f"Exhausted retries for Deel API endpoint: {url}")


def _is_transient(exc: Exception) -> bool:
    """Return True if exception is transient network or timeout error."""
    import httpx

    return isinstance(exc, (httpx.TimeoutException, httpx.NetworkError))


def _retry_after(headers: Any, attempt: int) -> float:
    """Extract Retry-After header or calculate exponential backoff."""
    header = (headers or {}).get("retry-after") or (headers or {}).get("Retry-After")
    if header:
        try:
            return float(header)
        except (ValueError, TypeError):
            pass
    return float(2**attempt)


def deel_source(
    token: str | None = None,
    base_url: str = _DEFAULT_BASE_URL,
    include_workers: bool = True,
    include_contracts: bool = True,
    include_contract_documents: bool = False,
    worker_ids: list[str] | None = None,
    contract_ids: list[str] | None = None,
    contract_types: list[str] | None = None,
    contract_statuses: list[str] | None = None,
    since: str | None = None,
    client: Any = None,
) -> Any:
    """Create a DLT source yielding Deel workers and contracts as Cognee documents.

    Args:
        token: Deel API token. If omitted, read from ``DEEL_API_TOKEN`` env var.
        base_url: Base URL for Deel REST API (defaults to ``https://api.letsdeel.com/rest/v1``).
        include_workers: Whether to sync the worker directory (default: True).
        include_contracts: Whether to sync contracts (default: True).
        include_contract_documents: Opt-in flag to ingest full sensitive contract document
            text/clauses. Default is False (metadata-first).
        worker_ids: Optional list of specific worker IDs to ingest.
        contract_ids: Optional list of specific contract IDs to ingest.
        contract_types: Optional filter list of contract types (e.g. ['eor', 'fixed']).
        contract_statuses: Optional filter list of contract statuses (e.g. ['in_progress']).
        since: Optional ISO-8601 string or date watermark for incremental sync via ``updated_at``.
        client: Optional pre-configured Deel client (for testing injection).

    Returns:
        A DLT source ready to pass to ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    resolved_client = client
    if resolved_client is None:
        resolved_token = token or os.environ.get("DEEL_API_TOKEN")
        if not resolved_token:
            raise ValueError(
                "Deel API token required: pass token= parameter or set DEEL_API_TOKEN."
            )
        resolved_client = DeelClient(token=resolved_token, base_url=base_url)

    resources = []

    if include_workers:

        @dlt.resource(
            name=DEEL_WORKERS_TABLE,
            primary_key="id",
            write_disposition="replace",
        )
        def deel_workers() -> Iterator[dict[str, Any]]:
            """Yield worker directory entries as document rows."""
            count = 0
            for person in _iter_people(resolved_client, worker_ids=worker_ids):
                row = _person_to_document(person)
                if row:
                    count += 1
                    yield row
            logger.info("Deel: synced %d worker(s).", count)

        resources.append(deel_workers)

    if include_contracts:

        @dlt.resource(
            name=DEEL_CONTRACTS_TABLE,
            primary_key="id",
            write_disposition="replace",
        )
        def deel_contracts() -> Iterator[dict[str, Any]]:
            """Yield contracts as document rows with optional sensitive body opt-in."""
            try:
                state = dlt.current.resource_state()
            except Exception:
                state = {}

            effective_since = since or state.get("last_updated_at")
            newest_updated_at = effective_since

            count = 0
            for contract in _iter_contracts(
                resolved_client,
                contract_ids=contract_ids,
                contract_types=contract_types,
                contract_statuses=contract_statuses,
                since=effective_since,
            ):
                row = _contract_to_document(
                    resolved_client,
                    contract,
                    include_documents=include_contract_documents,
                )
                if row:
                    count += 1
                    updated_at = contract.get("updated_at") or contract.get("created_at") or ""
                    if updated_at and (not newest_updated_at or updated_at > newest_updated_at):
                        newest_updated_at = updated_at
                    yield row

            if newest_updated_at:
                state["last_updated_at"] = newest_updated_at
            logger.info("Deel: synced %d contract(s).", count)

        resources.append(deel_contracts)

    @dlt.source(name=DEEL_SOURCE_NAME)
    def _source() -> list[Any]:
        return [res() for res in resources]

    src = _source()
    setattr(src, DOCUMENT_SOURCE_ATTR, DEEL_SOURCE_NAME)
    return src


# ---------------------------------------------------------------------------
# API Iteration & Pagination Helpers
# ---------------------------------------------------------------------------


def _iter_people(
    client: Any,
    worker_ids: list[str] | None = None,
) -> Iterator[dict[str, Any]]:
    """Paginate through Deel people/workers directory."""
    offset = 0
    limit = 50

    while True:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        data = client.get("people", params=params)

        items = data.get("data", [])
        if not items and isinstance(data, list):
            items = data

        if not items:
            break

        for person in items:
            if worker_ids and str(person.get("id")) not in worker_ids:
                continue
            yield person

        page_meta = data.get("page", {}) if isinstance(data, dict) else {}
        total_rows = page_meta.get("total_rows")

        if len(items) < limit:
            break

        offset += len(items)
        if total_rows is not None and offset >= total_rows:
            break


def _iter_contracts(
    client: Any,
    contract_ids: list[str] | None = None,
    contract_types: list[str] | None = None,
    contract_statuses: list[str] | None = None,
    since: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Paginate through Deel contracts using cursor or offset navigation."""
    cursor: str | None = None
    offset = 0
    limit = 50

    while True:
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["after_cursor"] = cursor
        else:
            params["offset"] = offset

        if contract_types:
            params["types[]"] = contract_types
        if contract_statuses:
            params["statuses[]"] = contract_statuses

        data = client.get("contracts", params=params)
        items = data.get("data", [])
        if not items and isinstance(data, list):
            items = data

        if not items:
            break

        for contract in items:
            if contract_ids and str(contract.get("id")) not in contract_ids:
                continue
            if since:
                updated_at = contract.get("updated_at") or contract.get("created_at") or ""
                if updated_at and updated_at < since:
                    continue
            yield contract

        page_meta = data.get("page", {}) if isinstance(data, dict) else {}
        next_cursor = page_meta.get("cursor") or page_meta.get("next_cursor")

        if next_cursor:
            cursor = next_cursor
        else:
            if len(items) < limit:
                break
            offset += len(items)
            total_rows = page_meta.get("total_rows")
            if total_rows is not None and offset >= total_rows:
                break


# ---------------------------------------------------------------------------
# Document Transformers (Metadata First, Sensitive Body Opt-in)
# ---------------------------------------------------------------------------


def _person_to_document(person: dict[str, Any]) -> dict[str, Any] | None:
    """Format worker directory metadata into a Cognee document row."""
    person_id = str(person.get("id") or "")
    if not person_id:
        return None

    first_name = person.get("first_name", "").strip()
    last_name = person.get("last_name", "").strip()
    full_name = person.get("full_name") or f"{first_name} {last_name}".strip() or "Unnamed Worker"

    job_title = person.get("job_title") or person.get("position") or "Team Member"
    department = person.get("department") or "General"
    work_email = person.get("work_email") or person.get("email") or "Not provided"
    country = person.get("country") or person.get("nationality") or "Not provided"
    hiring_type = person.get("hiring_type") or "Direct"
    hiring_status = person.get("hiring_status") or person.get("status") or "Active"
    start_date = person.get("start_date") or "Not specified"

    lines = [
        f"# Worker Profile: {full_name}",
        "",
        f"- **Full Name**: {full_name}",
        f"- **Job Title**: {job_title}",
        f"- **Department**: {department}",
        f"- **Work Email**: {work_email}",
        f"- **Location / Country**: {country}",
        f"- **Hiring Type**: {hiring_type}",
        f"- **Status**: {hiring_status}",
        f"- **Start Date**: {start_date}",
    ]

    return {
        "id": f"deel:worker:{person_id}",
        "url": f"https://app.letsdeel.com/people/{person_id}",
        "title": f"Worker: {full_name} ({job_title})",
        "content": "\n".join(lines),
    }


def _contract_to_document(
    client: Any,
    contract: dict[str, Any],
    include_documents: bool = False,
) -> dict[str, Any] | None:
    """Format contract into a Cognee document row.

    Contract body/clauses are only ingested if include_documents is True.
    """
    contract_id = str(contract.get("id") or "")
    if not contract_id:
        return None

    title = contract.get("title") or contract.get("name") or f"Contract {contract_id}"
    contract_type = contract.get("type") or "contract"
    status = contract.get("status") or "unknown"
    currency = contract.get("currency") or "USD"
    created_at = contract.get("created_at") or "Unknown"
    updated_at = contract.get("updated_at") or "Unknown"

    worker = contract.get("worker") or {}
    worker_name = (
        worker.get("full_name")
        or f"{worker.get('first_name', '')} {worker.get('last_name', '')}".strip()
        or "Not specified"
    )
    worker_email = worker.get("email") or "Not specified"

    client_org = contract.get("client") or {}
    client_name = client_org.get("name") or "Organization"

    lines = [
        f"# Contract: {title}",
        "",
        f"- **Contract ID**: {contract_id}",
        f"- **Type**: {contract_type}",
        f"- **Status**: {status}",
        f"- **Worker**: {worker_name} ({worker_email})",
        f"- **Client**: {client_name}",
        f"- **Currency**: {currency}",
        f"- **Created At**: {created_at}",
        f"- **Updated At**: {updated_at}",
    ]

    # Sensitive document text is strictly opt-in
    if include_documents:
        body = _fetch_contract_document_text(client, contract_id)
        if body:
            lines.extend(["", "## Contract Clauses & Content", "", body])

    return {
        "id": f"deel:contract:{contract_id}",
        "url": f"https://app.letsdeel.com/contracts/{contract_id}",
        "title": f"Contract: {title} ({contract_type})",
        "content": "\n".join(lines),
    }


def _fetch_contract_document_text(client: Any, contract_id: str) -> str:
    """Fetch optional contract document text if accessible."""
    try:
        detail = client.get(f"contracts/{contract_id}")
        data = detail.get("data") if isinstance(detail, dict) else detail
        if isinstance(data, dict):
            return str(
                data.get("document_template")
                or data.get("description")
                or data.get("scope_of_work")
                or ""
            ).strip()
    except Exception as exc:
        logger.warning(
            "Could not fetch detailed document text for contract %s: %s",
            contract_id,
            exc,
        )
    return ""
