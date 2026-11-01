"""Semrush data-source connector for cognee.

Syncs Semrush domain ranks and organic search keyword reports into cognee's
relational manifest path via dlt's declarative rest_api source with historical
period snapshots and API cost protection.
"""

from __future__ import annotations

import csv
import datetime
import hashlib
import io
import json
import logging
import os
import re
import urllib.parse
from collections.abc import Iterable
from typing import Any

import dlt
import requests
from cognee.tasks.ingestion.dlt_utils import PIPELINE_SCOPE_ATTR
from dlt.sources import DltSource
from dlt.sources.helpers.requests import Session
from dlt.sources.helpers.requests.retry import Client
from dlt.sources.rest_api import rest_api_resources

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants & Semrush Error Code Table
# ---------------------------------------------------------------------------

SEMRUSH_SOURCE_NAME = "semrush"
BASE_URL = "https://api.semrush.com"
SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Typed Exceptions & Classification Table
# ---------------------------------------------------------------------------


class SemrushError(Exception):
    """Base exception for all Semrush connector errors."""


class SemrushAuthError(SemrushError):
    """Raised when authentication fails (invalid, missing, or malformed API key)."""


class SemrushQuotaError(SemrushError):
    """Raised when Semrush API units or limits are exceeded."""


class SemrushNoDataError(SemrushError):
    """Raised when all requested domains return ERROR 50 (no data found)."""


class SemrushAPIError(SemrushError):
    """Raised when Semrush returns an unexpected API error code or server failure."""


# Official Semrush Analytics API error codes:
# Source: https://developer.semrush.com/api/v3/seo/overview#error-messages
#
# Verified error codes explicitly documented on the overview error-messages page:
#   50: Nothing found (handled as empty dataset [] for that domain)
#   70: API key not defined / hash failure (Auth)
#   110: Invalid key (Auth)
# Unverified / legacy codes reported across historical documentation or related APIs:
#   120: Wrong key-ID pair (Auth)
#   121: Wrong format or empty hash (Auth)
#   130: API disabled (Quota)
#   131: Limit exceeded (Quota)
#   132: API units balance is zero (Quota)
#   133: Database access denied (Quota)
#   134: Total limit exceeded (Quota)
#   135: API report type disabled (Quota)
#   136: Multiple limits not allowed (Quota)
# Note: Code 402 is omitted (HTTP payment code, not an Analytics API error code).
# Any unexpected or unlisted codes fall back dynamically to SemrushAPIError.
ERROR_CLASSIFICATION: dict[int, type[SemrushError]] = {
    70: SemrushAuthError,
    110: SemrushAuthError,
    120: SemrushAuthError,
    121: SemrushAuthError,
    130: SemrushQuotaError,
    131: SemrushQuotaError,
    132: SemrushQuotaError,
    133: SemrushQuotaError,
    134: SemrushQuotaError,
    135: SemrushQuotaError,
    136: SemrushQuotaError,
}

# ---------------------------------------------------------------------------
# Schema Type Hints for Gated Destination Tables
# ---------------------------------------------------------------------------

RANKS_SCHEMA_COLUMNS = {
    "domain": {"data_type": "text", "nullable": False},
    "database": {"data_type": "text", "nullable": False},
    "rank": {"data_type": "bigint", "nullable": True},
    "organic_keywords": {"data_type": "bigint", "nullable": True},
    "organic_traffic": {"data_type": "bigint", "nullable": True},
    "organic_cost": {"data_type": "double", "nullable": True},
    "adwords_keywords": {"data_type": "bigint", "nullable": True},
    "adwords_traffic": {"data_type": "bigint", "nullable": True},
    "adwords_cost": {"data_type": "double", "nullable": True},
    "snapshot_date": {"data_type": "text", "nullable": False},
}

ORGANIC_SCHEMA_COLUMNS = {
    "domain": {"data_type": "text", "nullable": False},
    "database": {"data_type": "text", "nullable": False},
    "keyword": {"data_type": "text", "nullable": False},
    "position": {"data_type": "bigint", "nullable": True},
    "previous_position": {"data_type": "bigint", "nullable": True},
    "position_difference": {"data_type": "bigint", "nullable": True},
    "search_volume": {"data_type": "bigint", "nullable": True},
    "cpc": {"data_type": "double", "nullable": True},
    "url": {"data_type": "text", "nullable": True},
    "traffic_percentage": {"data_type": "double", "nullable": True},
    "traffic_cost_percentage": {"data_type": "double", "nullable": True},
    "competition": {"data_type": "double", "nullable": True},
    "number_of_results": {"data_type": "bigint", "nullable": True},
    "trends": {"data_type": "text", "nullable": True},
    "snapshot_date": {"data_type": "text", "nullable": False},
}

# ---------------------------------------------------------------------------
# CSV Type Coercion & Field Mapping
# ---------------------------------------------------------------------------


def _coerce_val(val: Any, target_type: type, col_name: str) -> Any:
    """Coerce string CSV value to target_type (int, float, or str).

    Empty or whitespace strings return None. Non-empty values that fail to parse
    raise SemrushAPIError naming the failed column.
    """
    if val is None:
        return None
    s = str(val).strip()
    if s == "":
        return None
    if target_type is str:
        return s
    try:
        if target_type is int:
            return int(s)
        if target_type is float:
            return float(s)
    except (ValueError, TypeError) as exc:
        raise SemrushAPIError(
            f"Failed to coerce column '{col_name}' value {val!r} to {target_type.__name__}"
        ) from exc
    raise SemrushAPIError(
        f"Unsupported target type '{target_type.__name__}' for column '{col_name}'"
    )


RANKS_FIELD_MAP: dict[str, tuple[str, type]] = {
    "Db": ("database", str),
    "Dn": ("domain", str),
    "Rk": ("rank", int),
    "Or": ("organic_keywords", int),
    "Ot": ("organic_traffic", int),
    "Oc": ("organic_cost", float),
    "Ad": ("adwords_keywords", int),
    "At": ("adwords_traffic", int),
    "Ac": ("adwords_cost", float),
}

ORGANIC_FIELD_MAP: dict[str, tuple[str, type]] = {
    "Ph": ("keyword", str),
    "Po": ("position", int),
    "Pp": ("previous_position", int),
    "Pd": ("position_difference", int),
    "Nq": ("search_volume", int),
    "Cp": ("cpc", float),
    "Ur": ("url", str),
    "Tr": ("traffic_percentage", float),
    "Tc": ("traffic_cost_percentage", float),
    "Co": ("competition", float),
    "Nr": ("number_of_results", int),
    "Td": ("trends", str),
}

DOMAIN_RANKS_COLUMNS = ",".join(RANKS_FIELD_MAP.keys())
DOMAIN_ORGANIC_COLUMNS = ",".join(ORGANIC_FIELD_MAP.keys())

REPORT_FIELD_MAPS: dict[str, dict[str, tuple[str, type]]] = {
    "domain_ranks": RANKS_FIELD_MAP,
    "domain_organic": ORGANIC_FIELD_MAP,
}

# ---------------------------------------------------------------------------
# Key Redaction & Exception Sanitization
# ---------------------------------------------------------------------------


def sanitize_url(url: str | None, key: str | None = None) -> str | None:
    """Strip API key query parameter values and literal key matches from URL strings."""
    if not url:
        return url
    sanitized = re.sub(r"([?&]key=)[^& \'\"]+", r"\1[REDACTED]", url)
    target_key = key or os.environ.get("SEMRUSH_API_KEY")
    if target_key and target_key in sanitized:
        sanitized = sanitized.replace(target_key, "[REDACTED]")
    return sanitized


def sanitize_exception(exc: BaseException | None, key: str | None = None) -> None:
    """Recursively scrub sensitive key query parameters and literal key strings
    from an exception chain.
    """
    visited: set[int] = set()
    curr: BaseException | None = exc
    target_key = key or os.environ.get("SEMRUSH_API_KEY")

    def _scrub_str(s: str) -> str:
        res = sanitize_url(s, key=target_key)
        if res and target_key and target_key in res:
            res = res.replace(target_key, "[REDACTED]")
        return res or ""

    while curr is not None and id(curr) not in visited:
        visited.add(id(curr))
        if curr.args:
            curr.args = tuple(_scrub_str(arg) if isinstance(arg, str) else arg for arg in curr.args)
        if hasattr(curr, "url") and isinstance(curr.url, str):
            curr.url = _scrub_str(curr.url)
        resp = getattr(curr, "response", None)
        if resp is not None:
            if getattr(resp, "url", None) is not None:
                resp.url = _scrub_str(resp.url)
            req = getattr(resp, "request", None)
            if req is not None and getattr(req, "url", None) is not None:
                req.url = _scrub_str(req.url)
        req = getattr(curr, "request", None)
        if req is not None and getattr(req, "url", None) is not None:
            req.url = _scrub_str(req.url)

        if curr.__cause__ is not None and id(curr.__cause__) not in visited:
            curr = curr.__cause__
        elif curr.__context__ is not None and id(curr.__context__) not in visited:
            curr = curr.__context__
        else:
            break


class RedactingSession(Session):
    """Requests session that sanitizes API keys, applies dlt runtime-configured retries,
    and converts Semrush CSV responses and error codes into structured JSON payloads.
    """

    def __init__(
        self,
        *args: Any,
        api_key: str | None = None,
        max_attempts: int | None = None,
        backoff_factor: float | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.api_key = api_key
        client_kwargs: dict[str, Any] = {}
        if max_attempts is not None:
            client_kwargs["request_max_attempts"] = max_attempts
        if backoff_factor is not None:
            client_kwargs["request_backoff_factor"] = backoff_factor
        # Relies on dlt's private Client._make_session; verified on dlt 1.16.0 (floor)
        # and the locked version. Revisit if the dlt floor or major version changes.
        retry_runner = Client(**client_kwargs)._make_session().send.retry
        self._retry_send = retry_runner.wraps(super().send)

    def send(self, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        try:
            resp = self._retry_send(request, **kwargs)
        except Exception as exc:
            sanitize_exception(exc, key=self.api_key)
            raise

        if resp.url is not None:
            resp.url = sanitize_url(resp.url, key=self.api_key)
        if resp.request is not None and resp.request.url is not None:
            resp.request.url = sanitize_url(resp.request.url, key=self.api_key)

        # Check if response body is Semrush plain text / CSV
        content_type = resp.headers.get("Content-Type", "")
        is_text = "json" not in content_type.lower()
        if is_text and resp._content:
            text = resp._content.decode("utf-8", errors="replace").strip()
            match = re.match(r"^ERROR\s+(\d+)\s*::\s*(.*)$", text, re.DOTALL)
            if match:
                code = int(match.group(1))
                msg = match.group(2).strip()
                if code == 50:
                    # ERROR 50 represents 0 rows for this domain
                    resp._content = b"[]"
                    resp.headers["Content-Type"] = "application/json"
                else:
                    exc_cls = ERROR_CLASSIFICATION.get(code, SemrushAPIError)
                    raise exc_cls(f"Semrush error {code}: {msg}")
            elif resp.status_code < 400:
                url = (
                    resp.request.url
                    if resp.request is not None and resp.request.url
                    else (resp.url or "")
                )
                report_type = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get(
                    "type", [None]
                )[0]
                field_map = REPORT_FIELD_MAPS.get(report_type)
                if field_map is None:
                    raise SemrushAPIError(
                        f"Unknown or unsupported Semrush report type: {report_type!r}"
                    )

                expected_col_count = len(field_map)
                expected_keys = list(field_map.keys())

                reader = csv.reader(io.StringIO(text), delimiter=";")
                header = next(reader, None)
                if header is None or len(header) != expected_col_count:
                    got_count = len(header) if header is not None else 0
                    raise SemrushAPIError(
                        f"Unexpected column count in CSV header for report '{report_type}': "
                        f"expected {expected_col_count} columns, got {got_count}"
                    )

                rows: list[dict[str, str]] = []
                for line_num, r in enumerate(reader, start=2):
                    if not r or (len(r) == 1 and not r[0].strip()):
                        continue
                    if len(r) != expected_col_count:
                        raise SemrushAPIError(
                            f"Malformed CSV response on line {line_num}: "
                            f"expected {expected_col_count} columns, got {len(r)}"
                        )
                    rows.append(dict(zip(expected_keys, r, strict=True)))

                if not rows:
                    resp._content = b"[]"
                    resp.headers["Content-Type"] = "application/json"
                else:
                    resp._content = json.dumps(rows).encode("utf-8")
                    resp.headers["Content-Type"] = "application/json"

        if resp.status_code >= 400:
            try:
                resp.raise_for_status()
            except requests.HTTPError as exc:
                sanitize_exception(exc, key=self.api_key)
                raise

        return resp


# ---------------------------------------------------------------------------
# Period & Fingerprint Helpers
# ---------------------------------------------------------------------------


def default_monthly_period(today: datetime.date | None = None) -> str:
    """Return the default historical period (YYYYMM15) for the most recent closed month.

    Semrush historical analytics use the 15th of the month (YYYYMM15) to represent
    monthly snapshots (see https://developer.semrush.com/api/v3/seo/overview-reports/).
    Because the current month is still incomplete and active, the latest closed monthly period
    is always the 15th of the previous calendar month.
    """
    if today is None:
        today = datetime.date.today()
    first_of_month = today.replace(day=1)
    last_month_end = first_of_month - datetime.timedelta(days=1)
    return f"{last_month_end.year:04d}{last_month_end.month:02d}15"


def period_to_iso_date(period: str) -> str:
    """Convert a valid YYYYMM15 period string into an ISO date string (YYYY-MM-15)."""
    if not isinstance(period, str) or not re.match(r"^\d{4}(?:0[1-9]|1[0-2])15$", period):
        raise ValueError(
            f"Invalid period format: expected 'YYYYMM15' with a valid month, got {period!r}"
        )
    return f"{period[:4]}-{period[4:6]}-15"


def compute_selection_fingerprint(
    domains: list[str],
    database: str,
    display_limit: int,
) -> str:
    """Hash selection parameters into a deterministic SHA256 digest."""
    payload = {
        "database": database,
        "display_limit": display_limit,
        "domains": sorted(domains),
        "schema_version": SCHEMA_VERSION,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _map_row(
    raw_row: dict[str, Any],
    field_map: dict[str, tuple[str, type]],
    parent_domain: str,
    database: str,
    snapshot_date: str,
) -> dict[str, Any]:
    mapped: dict[str, Any] = {}
    for semrush_col, (dest_col, target_type) in field_map.items():
        mapped[dest_col] = _coerce_val(raw_row.get(semrush_col), target_type, dest_col)

    mapped["domain"] = parent_domain
    mapped["database"] = database
    mapped["snapshot_date"] = snapshot_date
    return mapped


# ---------------------------------------------------------------------------
# Declarative Source Factory
# ---------------------------------------------------------------------------


def semrush_source(
    domains: list[str],
    api_key: str | None = None,
    database: str = "us",
    display_limit: int = 10,
    display_date: str | None = None,
    session: requests.Session | None = None,
    force: bool = False,
) -> DltSource:
    """Create a dlt source for Semrush domain ranks and organic keywords.

    Custom sessions passed via session= are used as-is and must handle their own
    key redaction.
    """
    if isinstance(domains, str):
        raise ValueError("domains must be a list of strings, not a single string.")
    if not isinstance(domains, list):
        raise ValueError(f"domains must be a list of domain strings, got {type(domains).__name__}.")
    if not domains:
        raise ValueError("At least one domain must be specified in domains.")

    normalized_domains: list[str] = []
    seen: set[str] = set()
    for d in domains:
        if not isinstance(d, str):
            raise ValueError(f"All domains must be strings, got {type(d).__name__}: {d!r}")
        cleaned = d.strip().lower()
        if not cleaned:
            raise ValueError("Domain strings cannot be empty or whitespace.")
        if cleaned not in seen:
            seen.add(cleaned)
            normalized_domains.append(cleaned)

    if not normalized_domains:
        raise ValueError("At least one non-empty domain must be specified in domains.")

    if not isinstance(database, str) or not database.strip():
        raise ValueError("database must be a non-empty string.")
    normalized_database = database.strip().lower()

    if not isinstance(display_limit, int) or display_limit < 1:
        raise ValueError(f"display_limit must be a positive integer >= 1, got {display_limit!r}")

    if display_date is not None and (
        not isinstance(display_date, str)
        or not re.match(r"^\d{4}(?:0[1-9]|1[0-2])15$", display_date)
    ):
        raise ValueError(
            "display_date must be formatted as YYYYMM15 with a valid month "
            f"(e.g. '20260915'), got {display_date!r}"
        )

    resolved_key = api_key or os.environ.get("SEMRUSH_API_KEY")
    if not resolved_key:
        raise ValueError("Semrush API key required: pass api_key= or set SEMRUSH_API_KEY.")

    period = display_date or default_monthly_period()
    snapshot_date = period_to_iso_date(period)
    fingerprint = compute_selection_fingerprint(
        normalized_domains, normalized_database, display_limit
    )
    cursor = {"period": period, "selection_fingerprint": fingerprint}

    active_session = session or RedactingSession(api_key=resolved_key)
    auth_config = {
        "type": "api_key",
        "name": "key",
        "api_key": resolved_key,
        "location": "param",
    }

    # Parent resource: yields list of domain dicts for declarative rest_api resolution
    @dlt.resource(name="domains", selected=False)
    def domains_resource() -> Iterable[list[dict[str, str]]]:
        yield [{"domain": d} for d in sorted(normalized_domains)]

    ranks_endpoint_config = {
        "name": "domain_ranks",
        "endpoint": {
            "path": "",
            "params": {
                "type": "domain_ranks",
                "domain": "{resources.domains.domain}",
                "database": normalized_database,
                "display_limit": display_limit,
                "display_date": period,
                "export_columns": DOMAIN_RANKS_COLUMNS,
            },
            "paginator": "single_page",
            "data_selector": "$",
        },
        "include_from_parent": ["domain"],
    }

    organic_endpoint_config = {
        "name": "domain_organic",
        "endpoint": {
            "path": "",
            "params": {
                "type": "domain_organic",
                "domain": "{resources.domains.domain}",
                "database": normalized_database,
                "display_limit": display_limit,
                "display_date": period,
                "export_columns": DOMAIN_ORGANIC_COLUMNS,
            },
            "paginator": "single_page",
            "data_selector": "$",
        },
        "include_from_parent": ["domain"],
    }

    rest_config = {
        "client": {
            "base_url": BASE_URL,
            "auth": auth_config,
            "session": active_session,
        },
        "resources": [domains_resource, ranks_endpoint_config, organic_endpoint_config],
    }

    raw_resources = rest_api_resources(rest_config)
    res_map = {r.name: r for r in raw_resources}
    parent_domains_res = res_map["domains"]
    raw_ranks_res = res_map["domain_ranks"]
    raw_organic_res = res_map["domain_organic"]

    @dlt.source(name=SEMRUSH_SOURCE_NAME)
    def _semrush_source() -> Iterable[Any]:
        @dlt.resource(
            name="domain_ranks",
            write_disposition="replace",
            primary_key=["domain", "database"],
            columns=RANKS_SCHEMA_COLUMNS,
        )
        def domain_ranks_gated() -> Iterable[dict[str, Any]]:
            state = dlt.current.source_state()
            if not force and state.get("cursor_ranks") == cursor:
                # Unchanged selection and period: deselect resource to emit 0 load packages
                dlt.current.source().resources["domain_ranks"].selected = False
                return

            # Log estimated API units only when run actually fetches
            estimated_units = len(normalized_domains) * (1 + display_limit) * 50
            logger.info(
                "Fetching Semrush data for %d domain(s) (period=%s, display_limit=%d). "
                "Estimated cost: %d API units.",
                len(normalized_domains),
                period,
                display_limit,
                estimated_units,
            )

            total_yielded = 0
            for item in raw_ranks_res:
                row = dict(item)
                parent_domain = str(row.pop("_domains_domain", ""))
                yielded_row = _map_row(
                    raw_row=row,
                    field_map=RANKS_FIELD_MAP,
                    parent_domain=parent_domain,
                    database=normalized_database,
                    snapshot_date=snapshot_date,
                )
                total_yielded += 1
                yield yielded_row

            if total_yielded == 0:
                domains_str = ", ".join(normalized_domains)
                raise SemrushNoDataError(
                    f"No domain rank data found for domains [{domains_str}] in period {period}. "
                    "Try specifying an earlier display_date or verify domain spelling."
                )
            state["cursor_ranks"] = cursor

        @dlt.resource(
            name="domain_organic",
            write_disposition="replace",
            primary_key=["domain", "database", "keyword"],
            columns=ORGANIC_SCHEMA_COLUMNS,
        )
        def domain_organic_gated() -> Iterable[dict[str, Any]]:
            state = dlt.current.source_state()
            if not force and state.get("cursor_organic") == cursor:
                dlt.current.source().resources["domain_organic"].selected = False
                return

            for item in raw_organic_res:
                row = dict(item)
                parent_domain = str(row.pop("_domains_domain", ""))
                yielded_row = _map_row(
                    raw_row=row,
                    field_map=ORGANIC_FIELD_MAP,
                    parent_domain=parent_domain,
                    database=normalized_database,
                    snapshot_date=snapshot_date,
                )
                yield yielded_row

            # An empty domain_organic result is valid (domain has rank metrics
            # but no organic keywords).
            state["cursor_organic"] = cursor

        return [parent_domains_res, domain_ranks_gated, domain_organic_gated]

    source = _semrush_source()
    setattr(source, PIPELINE_SCOPE_ATTR, SEMRUSH_SOURCE_NAME)
    return source
