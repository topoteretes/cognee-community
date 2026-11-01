"""Clay data-source connector for cognee.

Sync a Clay table into cognee memory using Clay's Enterprise Table Query API
(``POST /public/v0/tables/query``). Built on ``dlt``'s declarative REST API source,
the resource produced here is passed directly to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_clay import clay_source

    await cognee.remember(
        clay_source(
            table_id="t_0te9i4tZEHwc9hihBXu",
            fields=["Company Name", "Domain", "Account Owner"],
            primary_key="domain",
        ),
        dataset_name="clay_accounts",
        write_disposition="replace",
        max_rows_per_table=0,
    )

Design
------
* **Authentication** — Authenticated via the ``clay-api-key`` HTTP header.
  The API key is passed via ``api_key`` argument or the ``CLAY_API_KEY``
  environment variable. The key is never logged.
* **Tier Requirement** — Clay's Table Query API is strictly an Enterprise feature.
  Workspaces on lower tiers (Free, Starter, Pro, Growth) will receive an HTTP 403
  with clear guidance to verify Enterprise plan access.
* **Declarative Pagination** — Clay's ``/tables/query`` endpoint paginates with a
  top-level cursor. This connector uses ``dlt``'s ``JSONResponseCursorPaginator``
  configured to read ``response["cursor"]`` and place it into ``request.json["cursor"]``.
* **Row Normalization** — Clay returns table cells as structured objects
  (e.g. ``{"status": "successful", "value": "Acme Corp"}``). The connector
  normalizes these into flat, clean dictionaries.
* **Primary Key & Identity** — If a system record ID is returned in the response,
  it is utilized. Alternatively, callers can designate a unique business key
  column (e.g. ``primary_key="domain"``), with fallback to a deterministic content
  hash. IDs are namespaced as ``clay:<table_id>:<row_id>``.
* **Data Licensing & Selective Sync** — Because Clay tables often contain data
  enriched by third-party providers under strict redistribution terms, callers
  are encouraged to specify the ``fields`` list to sync only their own first-party
  customer data.
"""

import hashlib
import json
import os
import re
from collections.abc import Callable
from typing import Any

Token = str | Callable[[], str]

try:
    from requests import Response
except ImportError:
    Response = Any  # type: ignore[misc,assignment]

try:
    from cognee.shared.logging_utils import get_logger

    logger = get_logger("clay_connector")
except ImportError:
    import logging

    logger = logging.getLogger("clay_connector")

DEFAULT_BASE_URL = "https://api.clay.com/public/v0"


def _normalize_field_name(field_name: str) -> str:
    """Convert an arbitrary column name into a clean snake_case identifier."""
    cleaned = re.sub(r"[^\w]+", "_", field_name.strip()).strip("_").lower()
    return cleaned or "col"


def _extract_cell_value(cell_data: Any) -> Any:
    """Extract unwrapped value from a structured Clay cell dictionary if present."""
    if isinstance(cell_data, dict):
        if "value" in cell_data:
            return cell_data["value"]
        if "status" in cell_data and set(cell_data.keys()).issubset(
            {"status", "error", "format", "type"}
        ):
            return None
    return cell_data


def _normalize_clay_row(
    raw_item: dict[str, Any],
    table_id: str,
    primary_key_field: str | None = None,
) -> dict[str, Any]:
    """Normalize a raw Clay record into a flat, typed dictionary with a stable ID.

    Handles both wrapped item formats (e.g. ``{"record_id": ..., "fields": {...}}``)
    and direct column mappings (e.g. ``{"Company Name": {"value": ..., "status": ...}}``).
    """
    if not isinstance(raw_item, dict):
        raise TypeError(f"Expected dict row from Clay API, got {type(raw_item).__name__}")

    raw_fields: dict[str, Any] = (
        raw_item.get("fields") if isinstance(raw_item.get("fields"), dict) else raw_item
    )

    normalized: dict[str, Any] = {}
    system_id: str | None = None

    # Check for top-level record identifier (unwrapping cell object if structured)
    for id_key in ("record_id", "id", "_id"):
        if id_key in raw_item and raw_item[id_key] is not None:
            extracted_id = _extract_cell_value(raw_item[id_key])
            if extracted_id is not None and str(extracted_id).strip():
                system_id = str(extracted_id).strip()
                break

    # Unpack cell values
    for col_name, cell_data in raw_fields.items():
        val = _extract_cell_value(cell_data)
        if (
            col_name in ("record_id", "id", "_id")
            and not system_id
            and val is not None
            and str(val).strip()
        ):
            system_id = str(val).strip()

        norm_key = _normalize_field_name(str(col_name))
        normalized[norm_key] = val

    # Resolve primary key: user-designated business key takes precedence over system ID
    if primary_key_field:
        norm_pk = _normalize_field_name(primary_key_field)
        val = normalized.get(norm_pk)
        if val is not None and str(val).strip():
            key_part = str(val).strip()
        elif system_id:
            key_part = system_id
        else:
            # Fall back to deterministic SHA-256 of normalized row when business key is null/empty
            hash_input = json.dumps(normalized, sort_keys=True, default=str)
            key_part = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()[:16]
    elif system_id:
        key_part = system_id
    else:
        # Deterministic SHA-256 fallback of normalized payload
        hash_input = json.dumps(normalized, sort_keys=True, default=str)
        key_part = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()[:16]

    normalized["id"] = f"clay:{table_id}:{key_part}"
    normalized["_deleted"] = False
    return normalized


def _handle_clay_response_errors(response: Response, *args: Any, **kwargs: Any) -> Response:
    """Inspect Clay HTTP response status and raise descriptive error messages."""
    if response.status_code == 400:
        try:
            err_json = response.json()
            detail = err_json.get("message") or err_json.get("error") or response.text
        except Exception:
            detail = response.text
        raise RuntimeError(
            f"Clay API bad request (HTTP 400): {detail}. "
            "Please check requested fields and query parameters."
        )
    if response.status_code == 401:
        raise RuntimeError(
            "Clay API authentication failed (HTTP 401). "
            "Please verify that your CLAY_API_KEY is valid and active."
        )
    if response.status_code == 403:
        raise RuntimeError(
            "Clay API access forbidden (HTTP 403). "
            "Clay's Table Query API is an Enterprise-only feature. "
            "Please verify that your workspace is on an Enterprise plan "
            "and that API access is enabled for this table under Table Settings -> Integrations."
        )
    if response.status_code == 404:
        raise RuntimeError(
            "Clay table not found (HTTP 404). "
            "Please verify that the table_id exists and is accessible."
        )
    if response.status_code == 429:
        raise RuntimeError(
            "Clay API rate limit exceeded (HTTP 429). "
            "Please reduce request frequency or increase retry backoff."
        )
    if response.status_code >= 500:
        raise RuntimeError(
            f"Clay API server error (HTTP {response.status_code}). "
            "Please try again later or check Clay service status."
        )
    return response


def clay_source(
    table_id: str | None = None,
    *,
    api_key: Token | None = None,
    fields: list[str] | None = None,
    primary_key: str | None = None,
    write_disposition: str = "replace",
    limit: int = 100,
    base_url: str = DEFAULT_BASE_URL,
    check_active: Callable[[], None] | None = None,
):
    """Return a ``dlt`` resource yielding normalized structured records from a Clay table.

    Args:
        table_id: Clay table ID to query (e.g. 't_0te9i4tZEHwc9hihBXu').
            Falls back to ``CLAY_TABLE_ID`` environment variable.
        api_key: Clay API key or callable returning one. Falls back to
            ``CLAY_API_KEY`` environment variable.
        fields: Optional list of column names to query. When specified, only these
            columns are selected via the query payload. If omitted, all table
            columns are fetched, and a warning is logged regarding third-party data licensing.
        primary_key: Optional business column name to use as the record identifier
            (e.g. 'domain', 'email'). Takes precedence over system IDs. If omitted or empty,
            system IDs or deterministic content hashes are used.
        write_disposition: ``dlt`` write disposition. Defaults to ``"replace"``
            (recommended full table snapshot sync).
        limit: Number of records to request per page (1-100, default 100).
        base_url: Base URL for Clay public API (default: 'https://api.clay.com/public/v0').
        check_active: Optional host authorization checkpoint called per row to allow
            clean cancellation without corrupted state.

    Returns:
        A ``dlt`` resource yielding normalized records with stable namespaced IDs.
    """
    try:
        from dlt.sources.rest_api import rest_api_source
    except ImportError as exc:
        raise ImportError(
            "The Clay connector requires the dlt[rest_api] extra. "
            'Install it with: pip install "cognee-community-connector-clay"'
        ) from exc

    raw_table_id = table_id or os.getenv("CLAY_TABLE_ID")
    resolved_table_id = raw_table_id.strip() if isinstance(raw_table_id, str) else None
    if not resolved_table_id:
        raise ValueError(
            "table_id is required "
            "(pass it explicitly or set the CLAY_TABLE_ID environment variable)."
        )

    raw_api_key = api_key or os.getenv("CLAY_API_KEY")
    if callable(raw_api_key):
        raw_api_key = raw_api_key()
    resolved_api_key = raw_api_key.strip() if isinstance(raw_api_key, str) else None
    if not resolved_api_key:
        raise ValueError(
            "api_key is required (pass it explicitly or set the CLAY_API_KEY environment variable)."
        )
    if any(c in resolved_api_key for c in "\r\n\t"):
        raise ValueError("api_key must not contain whitespace or control characters.")

    if not (1 <= limit <= 100):
        raise ValueError("limit must be between 1 and 100 as specified by Clay's Table Query API.")

    query_payload: dict[str, Any] = {
        "tables": [{"id": resolved_table_id}],
        "field_mode": "names",
    }

    if fields:
        sanitized_fields = [f.strip() for f in fields if isinstance(f, str) and f.strip()]
        query_payload["select"] = [
            {"field": f, "as": _normalize_field_name(f)} for f in sanitized_fields
        ]
    else:
        logger.warning(
            "No 'fields' filter specified for Clay table '%s'. Syncing all columns "
            "may include licensed third-party data subject to Clay Terms of Service "
            "export restrictions. Consider passing 'fields' to sync only your "
            "first-party customer data.",
            resolved_table_id,
        )

    config = {
        "client": {
            "base_url": base_url.rstrip("/"),
            "auth": {
                "type": "api_key",
                "api_key": resolved_api_key,
                "name": "clay-api-key",
                "location": "header",
            },
        },
        "resources": [
            {
                "name": f"clay_table_{_normalize_field_name(resolved_table_id)}",
                "write_disposition": write_disposition,
                "primary_key": "id",
                "endpoint": {
                    "path": "tables/query",
                    "method": "POST",
                    "json": {
                        "query": query_payload,
                        "limit": limit,
                    },
                    "paginator": {
                        "type": "cursor",
                        "cursor_path": "cursor",
                        "cursor_body_path": "cursor",
                    },
                    "response_actions": [_handle_clay_response_errors],
                    "data_selector": "data",
                },
            }
        ],
    }

    source = rest_api_source(config)
    resource_name = f"clay_table_{_normalize_field_name(resolved_table_id)}"
    resource = source.resources[resource_name]

    # Apply row normalization transformation mapping
    def _transform_row(row: dict[str, Any]) -> dict[str, Any]:
        if check_active is not None:
            check_active()
        return _normalize_clay_row(
            row,
            table_id=resolved_table_id,
            primary_key_field=primary_key,
        )

    resource.add_map(_transform_row)

    # Note: DOCUMENT_SOURCE_ATTR is deliberately NOT set on this resource,
    # ensuring cognee routes these records through structured relational/graph ingestion
    # rather than unstructured document chunking.
    return resource
