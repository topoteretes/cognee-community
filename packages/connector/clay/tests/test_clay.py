"""100% offline unit tests for the Clay data connector."""

import json
from typing import Any
from unittest.mock import patch

import pytest
from requests import Response

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"
from cognee_community_connector_clay.clay import (
    _extract_cell_value,
    _normalize_clay_row,
    _normalize_field_name,
    clay_source,
)


def _make_mock_response(status_code: int, json_data: dict[str, Any]) -> Response:
    """Helper to construct a mock requests.Response object."""
    resp = Response()
    resp.status_code = status_code
    resp._content = json.dumps(json_data).encode("utf-8")
    resp.headers["Content-Type"] = "application/json"
    return resp


def test_missing_required_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    """Validate that missing table_id or api_key raises ValueError."""
    monkeypatch.delenv("CLAY_TABLE_ID", raising=False)
    monkeypatch.delenv("CLAY_API_KEY", raising=False)

    with pytest.raises(ValueError, match="table_id is required"):
        clay_source(table_id=None, api_key="secret")

    with pytest.raises(ValueError, match="api_key is required"):
        clay_source(table_id="t_123", api_key=None)


def test_invalid_limit(sample_table_id: str, sample_api_key: str) -> None:
    """Validate that limit < 1 or limit > 100 raises ValueError."""
    with pytest.raises(ValueError, match="limit must be between 1 and 100"):
        clay_source(table_id=sample_table_id, api_key=sample_api_key, limit=0)

    with pytest.raises(ValueError, match="limit must be between 1 and 100"):
        clay_source(table_id=sample_table_id, api_key=sample_api_key, limit=101)


def test_normalize_field_name() -> None:
    """Verify column name normalization into snake_case identifiers."""
    assert _normalize_field_name("Company Name") == "company_name"
    assert _normalize_field_name("  Account-Owner  ") == "account_owner"
    assert _normalize_field_name("ARR") == "arr"
    assert _normalize_field_name("Email (Work)") == "email_work"
    assert _normalize_field_name("Revenue ($)") == "revenue"


def test_single_page_ingestion(
    sample_table_id: str,
    sample_api_key: str,
    mock_single_page_data: dict[str, Any],
) -> None:
    """Test full single-page ingestion with cell unwrapping and stable row ID."""
    calls = []

    def mock_send(self, request, **kwargs):
        calls.append(request)
        return _make_mock_response(200, mock_single_page_data)

    with patch("requests.adapters.HTTPAdapter.send", new=mock_send):
        resource = clay_source(
            table_id=sample_table_id,
            api_key=sample_api_key,
            write_disposition="replace",
        )

        # Structured tabular contract: DOCUMENT_SOURCE_ATTR is NOT set
        assert getattr(resource, DOCUMENT_SOURCE_ATTR, None) is None

        rows = list(resource)
        assert len(rows) == 2

        # Verify cell unwrapping from {"value": ..., "status": ...}
        row1 = rows[0]
        assert row1["id"] == f"clay:{sample_table_id}:rec_001"
        assert row1["company_name"] == "Acme Corp"
        assert row1["domain"] == "acme.com"
        assert row1["arr"] == 150000
        assert row1["_deleted"] is False

        row2 = rows[1]
        assert row2["id"] == f"clay:{sample_table_id}:rec_002"
        assert row2["company_name"] == "Globex Inc"
        assert row2["arr"] is None

        # Verify authentication header was sent
        assert len(calls) == 1
        assert calls[0].headers["clay-api-key"] == sample_api_key


def test_multi_page_cursor_pagination(
    sample_table_id: str,
    sample_api_key: str,
    mock_multi_page_data_p1: dict[str, Any],
    mock_multi_page_data_p2: dict[str, Any],
) -> None:
    """Test that cursor from response body is placed into request body of next page."""
    sent_payloads = []

    def mock_send(self, request, **kwargs):
        payload = json.loads(request.body.decode("utf-8")) if request.body else {}
        sent_payloads.append(payload)

        if len(sent_payloads) == 1:
            return _make_mock_response(200, mock_multi_page_data_p1)
        return _make_mock_response(200, mock_multi_page_data_p2)

    with patch("requests.adapters.HTTPAdapter.send", new=mock_send):
        resource = clay_source(
            table_id=sample_table_id,
            api_key=sample_api_key,
        )

        rows = list(resource)
        assert len(rows) == 2
        assert len(sent_payloads) == 2

        # Page 1 payload has no cursor
        assert "cursor" not in sent_payloads[0]

        # Page 2 payload must have cursor injected by JSONResponseCursorPaginator
        assert sent_payloads[1]["cursor"] == "cursor_token_page_2"

        # Verify records from both pages are present
        assert rows[0]["company_name"] == "Stark Industries"
        assert rows[1]["company_name"] == "Wayne Enterprises"


def test_fields_filter_payload(sample_table_id: str, sample_api_key: str) -> None:
    """Test that passing fields populates query.select array in the POST payload."""
    sent_payloads = []

    def mock_send(self, request, **kwargs):
        payload = json.loads(request.body.decode("utf-8")) if request.body else {}
        sent_payloads.append(payload)
        return _make_mock_response(200, {"data": [], "cursor": None})

    with patch("requests.adapters.HTTPAdapter.send", new=mock_send):
        resource = clay_source(
            table_id=sample_table_id,
            api_key=sample_api_key,
            fields=["Company Name", "Domain"],
        )
        list(resource)

        assert len(sent_payloads) == 1
        query = sent_payloads[0]["query"]
        assert "select" in query
        assert query["select"] == [
            {"field": "Company Name", "as": "company_name"},
            {"field": "Domain", "as": "domain"},
        ]


def test_primary_key_strategies(sample_table_id: str) -> None:
    """Test system record ID, custom business column, and strict validation when missing."""
    # 1. System ID present
    row_with_sys_id = {"record_id": "rec_999", "name": "Test"}
    norm1 = _normalize_clay_row(row_with_sys_id, table_id=sample_table_id)
    assert norm1["id"] == f"clay:{sample_table_id}:rec_999"

    # 2. Custom business column designated as primary_key takes precedence over system record_id
    row_with_both = {
        "record_id": "rec_999",
        "domain": "alpha.io",
        "company_name": "Alpha IO",
    }
    norm2 = _normalize_clay_row(
        row_with_both,
        table_id=sample_table_id,
        primary_key_field="domain",
    )
    assert norm2["id"] == f"clay:{sample_table_id}:alpha.io"

    # 3. Custom business column with null value falls back to system record_id if available
    row_with_null_pk = {
        "record_id": "rec_999",
        "domain": None,
        "company_name": "Alpha IO",
    }
    norm_fallback = _normalize_clay_row(
        row_with_null_pk,
        table_id=sample_table_id,
        primary_key_field="domain",
    )
    assert norm_fallback["id"] == f"clay:{sample_table_id}:rec_999"

    # 4. Row with neither system ID nor primary_key raises ValueError
    row_plain = {"company_name": "No ID Co"}
    with pytest.raises(ValueError, match="has no stable system record identifier"):
        _normalize_clay_row(row_plain, table_id=sample_table_id)

    # 5. Row with designated primary_key but null value and no system ID raises ValueError
    row_no_id_null_pk = {"company_name": "No ID Co", "domain": None}
    with pytest.raises(ValueError, match="missing designated primary_key"):
        _normalize_clay_row(
            row_no_id_null_pk,
            table_id=sample_table_id,
            primary_key_field="domain",
        )


def test_empty_table_ingestion(sample_table_id: str, sample_api_key: str) -> None:
    """Test querying an empty table yields 0 records without errors."""

    def mock_send(self, request, **kwargs):
        return _make_mock_response(200, {"data": [], "cursor": None})

    with patch("requests.adapters.HTTPAdapter.send", new=mock_send):
        resource = clay_source(table_id=sample_table_id, api_key=sample_api_key)
        rows = list(resource)
        assert rows == []


def test_authentication_error_401(sample_table_id: str, sample_api_key: str) -> None:
    """Test HTTP 401 response raises descriptive RuntimeError for API key."""

    def mock_send(self, request, **kwargs):
        return _make_mock_response(401, {"error": "Unauthorized"})

    with patch("requests.adapters.HTTPAdapter.send", new=mock_send):
        resource = clay_source(table_id=sample_table_id, api_key=sample_api_key)
        with pytest.raises(Exception, match="Clay API authentication failed"):
            list(resource)


def test_enterprise_tier_error_403(sample_table_id: str, sample_api_key: str) -> None:
    """Test HTTP 403 response raises descriptive RuntimeError with Enterprise guidance."""

    def mock_send(self, request, **kwargs):
        return _make_mock_response(403, {"error": "Forbidden"})

    with patch("requests.adapters.HTTPAdapter.send", new=mock_send):
        resource = clay_source(table_id=sample_table_id, api_key=sample_api_key)
        with pytest.raises(Exception, match="Enterprise-only feature"):
            list(resource)


def test_api_key_security(sample_table_id: str, sample_api_key: str) -> None:
    """Test that api_key is never exposed in resource string representation."""
    resource = clay_source(table_id=sample_table_id, api_key=sample_api_key)
    res_str = str(resource)
    res_repr = repr(resource)

    assert sample_api_key not in res_str
    assert sample_api_key not in res_repr


def test_primary_key_null_and_blank_validation(sample_table_id: str) -> None:
    """Verify that null/empty/blank business keys without system ID raise ValueError."""
    row_a = {"company_name": "Alpha Corp", "domain": None}
    row_b = {"company_name": "Beta Corp", "domain": ""}
    row_c = {"company_name": "Gamma Corp", "domain": "   "}

    for invalid_row in (row_a, row_b, row_c):
        with pytest.raises(ValueError, match="missing designated primary_key"):
            _normalize_clay_row(
                invalid_row,
                table_id=sample_table_id,
                primary_key_field="domain",
            )

    # When system ID is present, null/blank business key falls back to system ID safely
    row_with_fallback = {"company_name": "Delta Corp", "domain": "  ", "record_id": "rec_delta"}
    norm = _normalize_clay_row(
        row_with_fallback,
        table_id=sample_table_id,
        primary_key_field="domain",
    )
    assert norm["id"] == f"clay:{sample_table_id}:rec_delta"

    # Blank primary_key parameter in clay_source must fail fast
    with pytest.raises(ValueError, match="primary_key cannot be an empty or blank string"):
        clay_source(table_id=sample_table_id, api_key="secret", primary_key="   ")


def test_structured_cell_system_id(sample_table_id: str) -> None:
    """Verify system IDs wrapped in structured cell objects are extracted cleanly."""
    row_wrapped_record_id = {
        "record_id": {"value": "rec_struct_001", "status": "success"},
        "company_name": "Delta Co",
    }
    norm1 = _normalize_clay_row(row_wrapped_record_id, table_id=sample_table_id)
    assert norm1["id"] == f"clay:{sample_table_id}:rec_struct_001"
    assert "status" not in norm1["id"]

    row_field_level_id = {
        "fields": {
            "id": {"value": "rec_struct_002", "status": "success"},
            "company_name": "Epsilon Inc",
        }
    }
    norm2 = _normalize_clay_row(row_field_level_id, table_id=sample_table_id)
    assert norm2["id"] == f"clay:{sample_table_id}:rec_struct_002"
    assert "status" not in norm2["id"]


def test_non_dict_row_validation(sample_table_id: str) -> None:
    """Verify that a malformed non-dictionary row raises TypeError."""
    with pytest.raises(TypeError, match="Expected dict row from Clay API"):
        _normalize_clay_row("not-a-dict", table_id=sample_table_id)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="Expected dict row from Clay API"):
        _normalize_clay_row(["item"], table_id=sample_table_id)  # type: ignore[arg-type]


def test_http_400_bad_request(sample_table_id: str, sample_api_key: str) -> None:
    """Test HTTP 400 response raises descriptive RuntimeError for bad query."""

    def mock_send(self, request, **kwargs):
        return _make_mock_response(400, {"message": "Invalid select column: UnknownField"})

    with patch("requests.adapters.HTTPAdapter.send", new=mock_send):
        resource = clay_source(table_id=sample_table_id, api_key=sample_api_key)
        with pytest.raises(Exception, match=r"Clay API bad request .*Invalid select column"):
            list(resource)


def test_http_404_not_found(sample_table_id: str, sample_api_key: str) -> None:
    """Test HTTP 404 response raises descriptive RuntimeError for table not found."""

    def mock_send(self, request, **kwargs):
        return _make_mock_response(404, {"error": "Table not found"})

    with patch("requests.adapters.HTTPAdapter.send", new=mock_send):
        resource = clay_source(table_id=sample_table_id, api_key=sample_api_key)
        with pytest.raises(Exception, match="Clay table not found"):
            list(resource)


def test_http_429_rate_limit(sample_table_id: str, sample_api_key: str) -> None:
    """Test HTTP 429 response raises descriptive RuntimeError for rate limit."""

    def mock_send(self, request, **kwargs):
        return _make_mock_response(429, {"error": "Rate limit exceeded"})

    with patch("requests.adapters.HTTPAdapter.send", new=mock_send):
        resource = clay_source(table_id=sample_table_id, api_key=sample_api_key)
        with pytest.raises(Exception, match="Clay API rate limit exceeded"):
            list(resource)


def test_extract_cell_value_all_statuses() -> None:
    """Verify that all documented Clay cell statuses are extracted or unwrapped correctly."""
    # 1. Success status
    assert _extract_cell_value({"status": "success", "value": "Acme"}) == "Acme"
    assert _extract_cell_value({"status": "successful", "value": "Acme"}) == "Acme"
    assert _extract_cell_value({"status": "success", "value": 0}) == 0
    assert _extract_cell_value({"status": "success", "value": False}) is False

    # 2. Empty status (evaluated but no value)
    assert _extract_cell_value({"status": "empty"}) is None
    assert _extract_cell_value({"status": "empty", "value": None}) is None

    # 3. Error status (enrichment or calculation failed)
    assert _extract_cell_value({"status": "error", "error": "Provider timeout"}) is None
    assert _extract_cell_value({"status": "error", "error": "Not found", "type": "lookup"}) is None

    # 4. Running status (enrichment in progress)
    assert _extract_cell_value({"status": "running"}) is None

    # 5. Non-cell dictionary preserving arbitrary fields
    arbitrary_dict = {"status": "active", "priority": "high", "score": 95}
    assert _extract_cell_value(arbitrary_dict) == arbitrary_dict

    # 6. Primitive values pass through
    assert _extract_cell_value("simple_string") == "simple_string"
    assert _extract_cell_value(42) == 42
    assert _extract_cell_value(None) is None


def test_cell_statuses_ingestion(
    sample_table_id: str,
    sample_api_key: str,
    mock_cell_statuses_data: dict[str, Any],
) -> None:
    """Test full table ingestion with rows containing success, empty, error, and running cells."""

    def mock_send(self, request, **kwargs):
        return _make_mock_response(200, mock_cell_statuses_data)

    with patch("requests.adapters.HTTPAdapter.send", new=mock_send):
        resource = clay_source(table_id=sample_table_id, api_key=sample_api_key)
        rows = list(resource)

        assert len(rows) == 1
        row = rows[0]
        assert row["id"] == f"clay:{sample_table_id}:rec_multi_status_01"
        assert row["company_name"] == "Status Corp"
        assert row["domain"] == "status.io"
        assert row["funding"] is None  # empty status
        assert row["enrichment"] is None  # error status
        assert row["verified"] is None  # running status


def test_exceed_max_fields_raises_error(sample_table_id: str, sample_api_key: str) -> None:
    """Test that specifying more than 20 fields raises ValueError locally."""
    too_many_fields = [f"Field_{i}" for i in range(21)]
    with pytest.raises(ValueError, match="allows a maximum of 20 selected fields"):
        clay_source(
            table_id=sample_table_id,
            api_key=sample_api_key,
            fields=too_many_fields,
        )


def test_empty_fields_list_rejected(sample_table_id: str, sample_api_key: str) -> None:
    """Test that passing an empty fields list fails fast instead of syncing all columns."""
    with pytest.raises(ValueError, match="fields cannot be an empty list"):
        clay_source(
            table_id=sample_table_id,
            api_key=sample_api_key,
            fields=[],
        )

    with pytest.raises(ValueError, match="fields cannot be an empty list"):
        clay_source(
            table_id=sample_table_id,
            api_key=sample_api_key,
            fields=["   ", ""],
        )


def test_http_422_unprocessable_entity(sample_table_id: str, sample_api_key: str) -> None:
    """Test HTTP 422 response raises descriptive RuntimeError for invalid payload or schema."""

    def mock_send(self, request, **kwargs):
        return _make_mock_response(422, {"message": "Invalid field: NonExistentColumn"})

    with patch("requests.adapters.HTTPAdapter.send", new=mock_send):
        resource = clay_source(table_id=sample_table_id, api_key=sample_api_key)
        with pytest.raises(
            Exception,
            match=r"Clay API unprocessable entity \(HTTP 422\): Invalid field: NonExistentColumn",
        ):
            list(resource)


def test_api_key_callable_and_whitespace_sanitization(
    sample_table_id: str, mock_single_page_data: dict[str, Any]
) -> None:
    """Test that api_key as a callable and with whitespace padding is supported."""
    calls = []

    def mock_send(self, request, **kwargs):
        calls.append(request)
        return _make_mock_response(200, mock_single_page_data)

    with patch("requests.adapters.HTTPAdapter.send", new=mock_send):
        resource = clay_source(
            table_id=f"  {sample_table_id}  ",
            api_key=lambda: "  secret_key_callable_123  ",
        )
        rows = list(resource)
        assert len(rows) == 2
        assert calls[0].headers["clay-api-key"] == "secret_key_callable_123"


def test_api_key_invalid_control_characters(sample_table_id: str) -> None:
    """Test that api_key containing newline or tab raises ValueError."""
    with pytest.raises(ValueError, match="must not contain whitespace or control characters"):
        clay_source(table_id=sample_table_id, api_key="secret\nkey")

    with pytest.raises(ValueError, match="table_id is required"):
        clay_source(table_id="   ", api_key="valid_key")


def test_check_active_cancellation(
    sample_table_id: str, sample_api_key: str, mock_single_page_data: dict[str, Any]
) -> None:
    """Test check_active callback can cancel extraction cleanly."""

    def mock_send(self, request, **kwargs):
        return _make_mock_response(200, mock_single_page_data)

    def mock_check_active():
        raise InterruptedError("Sync cancelled by host")

    with patch("requests.adapters.HTTPAdapter.send", new=mock_send):
        resource = clay_source(
            table_id=sample_table_id,
            api_key=sample_api_key,
            check_active=mock_check_active,
        )
        with pytest.raises(Exception, match=r"Sync cancelled by host"):
            list(resource)


def test_http_500_server_error(sample_table_id: str, sample_api_key: str) -> None:
    """Test HTTP 500 response raises descriptive RuntimeError for Clay service outage."""

    def mock_send(self, request, **kwargs):
        return _make_mock_response(500, {"error": "Internal Server Error"})

    with patch("requests.adapters.HTTPAdapter.send", new=mock_send):
        resource = clay_source(table_id=sample_table_id, api_key=sample_api_key)
        with pytest.raises(Exception, match=r"Clay API server error .*500"):
            list(resource)
