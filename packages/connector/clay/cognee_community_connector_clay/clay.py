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
from typing import Any

from requests import Response

try:
    from cognee.shared.logging_utils import get_logger

    logger = get_logger("clay_connector")
except ImportError:
    import logging

    logger = logging.getLogger("clay_connector")

DEFAULT_BASE_URL = "https://api.clay.com/public/v0"


def _normalize_field_name(field_name: str) -> str:
    """Convert an arbitrary column name into a clean snake_case identifier."""
    return field_name.strip().lower().replace(" ", "_").replace("-", "_")


def _normalize_clay_row(
    raw_item: dict[str, Any],
    table_id: str,
    primary_key_field: str | None = None,
) -> dict[str, Any]:
    """Normalize a raw Clay record into a flat, typed dictionary with a stable ID.

    Handles both wrapped item formats (e.g. ``{"record_id": ..., "fields": {...}}``)
    and direct column mappings (e.g. ``{"Company Name": {"value": ..., "status": ...}}``).
    """
    raw_fields: dict[str, Any] = (
        raw_item.get("fields") if isinstance(raw_item.get("fields"), dict) else raw_item
    )

    normalized: dict[str, Any] = {}
    system_id: str | None = None

    # Check for top-level or field-level record identifier
    for id_key in ("record_id", "id", "_id"):
        if id_key in raw_item and raw_item[id_key] is not None:
            system_id = str(raw_item[id_key])
            break

    # Unpack cell values
    for col_name, cell_data in raw_fields.items():
        if col_name in ("record_id", "id", "_id") and not system_id:
            system_id = str(cell_data)

        norm_key = _normalize_field_name(str(col_name))

        if isinstance(cell_data, dict) and ("value" in cell_data or "status" in cell_data):
            normalized[norm_key] = cell_data.get("value")
        else:
            normalized[norm_key] = cell_data

    # Resolve primary key
    if system_id:
        key_part = system_id
    elif primary_key_field and _normalize_field_name(primary_key_field) in normalized:
        resolved_val = normalized[_normalize_field_name(primary_key_field)]
        key_part = str(resolved_val) if resolved_val is not None else "null"
    else:
        # Deterministic SHA-256 fallback of normalized payload
        hash_input = json.dumps(normalized, sort_keys=True, default=str)
        key_part = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()[:16]

    normalized["id"] = f"clay:{table_id}:{key_part}"
    normalized["_deleted"] = False
    return normalized


def _handle_clay_response_errors(response: Response, *args: Any, **kwargs: Any) -> Response:
    """Inspect Clay HTTP response status and raise descriptive error messages."""
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
    return response


def clay_source(
    table_id: str | None = None,
    *,
    api_key: str | None = None,
    fields: list[str] | None = None,
    primary_key: str | None = None,
    write_disposition: str = "replace",
    limit: int = 100,
    base_url: str = DEFAULT_BASE_URL,
):
    """Return a ``dlt`` resource yielding normalized structured records from a Clay table.

    Args:
        table_id: Clay table ID to query (e.g. 't_0te9i4tZEHwc9hihBXu').
            Falls back to ``CLAY_TABLE_ID`` environment variable.
        api_key: Clay API key. Falls back to ``CLAY_API_KEY`` environment variable.
        fields: Optional list of column names to query. When specified, only these
            columns are selected via the query payload. If omitted, all table
            columns are fetched, and a warning is logged regarding third-party data licensing.
        primary_key: Optional business column name to use as the record identifier
            (e.g. 'domain', 'email'). If omitted, system IDs or deterministic content
            hashes are used.
        write_disposition: ``dlt`` write disposition. Defaults to ``"replace"``
            (recommended full table snapshot sync).
        limit: Number of records to request per page (1-100, default 100).
        base_url: Base URL for Clay public API (default: 'https://api.clay.com/public/v0').

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

    resolved_table_id = table_id or os.getenv("CLAY_TABLE_ID")
    if not resolved_table_id:
        raise ValueError(
            "table_id is required "
            "(pass it explicitly or set the CLAY_TABLE_ID environment variable)."
        )

    resolved_api_key = api_key or os.getenv("CLAY_API_KEY")
    if not resolved_api_key:
        raise ValueError(
            "api_key is required (pass it explicitly or set the CLAY_API_KEY environment variable)."
        )

    if not (1 <= limit <= 100):
        raise ValueError("limit must be between 1 and 100 as specified by Clay's Table Query API.")

    query_payload: dict[str, Any] = {
        "tables": [{"id": resolved_table_id}],
        "field_mode": "names",
    }

    if fields:
        query_payload["select"] = [{"field": f, "as": _normalize_field_name(f)} for f in fields]
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
    resource.add_map(
        lambda row: _normalize_clay_row(
            row,
            table_id=resolved_table_id,
            primary_key_field=primary_key,
        )
    )

    # Note: DOCUMENT_SOURCE_ATTR is deliberately NOT set on this resource,
    # ensuring cognee routes these records through structured relational/graph ingestion
    # rather than unstructured document chunking.
    return resource
