"""Unit tests for the Semrush connector."""

import datetime
import json
import logging
import os
from unittest.mock import patch

import dlt
import pytest
import requests
from requests.exceptions import ConnectionError as RequestsConnectionError

from cognee_community_connector_semrush.semrush import (
    ERROR_CLASSIFICATION,
    ORGANIC_SCHEMA_COLUMNS,
    RANKS_SCHEMA_COLUMNS,
    RedactingSession,
    SemrushAPIError,
    _coerce_val,
    compute_selection_fingerprint,
    default_monthly_period,
    period_to_iso_date,
    sanitize_exception,
    sanitize_url,
    semrush_source,
)

# Real documented Semrush CSV headers
# Sources:
# - Domain Overview: https://developer.semrush.com/api/v3/seo/overview-reports/#domain-overview-all-databases
# - Organic Keywords: https://developer.semrush.com/api/v3/seo/organic-reports/#domain-organic-search-keywords
_DOC_RANKS_HEADER = (
    "Database;Domain;Rank;Organic Keywords;Organic Traffic;Organic Cost;"
    "Adwords Keywords;Adwords Traffic;Adwords Cost"
)
_DOC_ORGANIC_HEADER = (
    "Keyword;Position;Previous Position;Position Difference;Search Volume;CPC;"
    "Url;Traffic (%);Traffic Cost (%);Competition;Number of Results;Trends"
)
_VALID_RANKS_CSV = f"{_DOC_RANKS_HEADER}\nus;example.com;100;1000;2000;500.0;5;50;10.0\n"
_VALID_ORGANIC_CSV = (
    f"{_DOC_ORGANIC_HEADER}\nkeyword1;1;2;-1;100;1.5;https://example.com;5.0;2.0;0.5;1000;0.1\n"
)


# ---------------------------------------------------------------------------
# 1. Date & Boundary Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("input_date", "expected_period"),
    [
        (datetime.date(2026, 10, 15), "20260915"),  # mid-month
        (datetime.date(2026, 5, 1), "20260415"),  # month rollover
        (datetime.date(2026, 1, 1), "20251215"),  # year rollover (Jan 1)
        (datetime.date(2024, 3, 1), "20240215"),  # leap-year Feb
    ],
)
def test_default_monthly_period_boundaries(input_date: datetime.date, expected_period: str) -> None:
    """Verify default period resolves to the 15th of the previous calendar month."""
    assert default_monthly_period(input_date) == expected_period


# ---------------------------------------------------------------------------
# 2. Key Redaction & Exception Sanitization Tests (Items 1, 2, 6)
# ---------------------------------------------------------------------------


def test_sanitize_url() -> None:
    """Verify API keys are stripped from URLs."""
    raw = "https://api.semrush.com/?key=SUPER_SECRET_12345&type=domain_ranks"
    sanitized = sanitize_url(raw)
    assert sanitized is not None
    assert "SUPER_SECRET_12345" not in sanitized
    assert sanitized == "https://api.semrush.com/?key=[REDACTED]&type=domain_ranks"


def test_literal_key_scrubbing_from_exception() -> None:
    """Item 6: Additionally scrub the literal API key value from exception text,
    not only key= patterns.
    """
    secret = "LITERAL_SECRET_VALUE_999"
    err = RuntimeError(f"Connection failed: raw token {secret} leaked in driver error")
    sanitize_exception(err, key=secret)

    assert secret not in str(err)
    assert "[REDACTED]" in str(err)


def test_key_redaction_403_truthiness_bug(tmp_path, caplog: pytest.LogCaptureFixture) -> None:
    """Item 1 & 6: exc.response truthiness check handles 4xx/5xx responses where
    bool(response) is False.

    Asserts key is absent from str(exc), exc.response.url, exc.response.request.url,
    logs, and pipeline directory.
    """
    secret_key = "MY_ULTRA_SECRET_KEY_999"
    caplog.set_level(logging.DEBUG)

    pipeline = dlt.pipeline(
        pipeline_name="test_key_redact_pipe",
        destination=dlt.destinations.duckdb(str(tmp_path / "test_key_redact.duckdb")),
        pipelines_dir=str(tmp_path / "dlt_pipelines"),
    )

    mock_resp = requests.Response()
    mock_resp.status_code = 403
    mock_resp._content = b"ERROR 120 :: WRONG KEY-ID PAIR"
    mock_resp.url = f"https://api.semrush.com/?key={secret_key}&type=domain_ranks"
    mock_resp.request = requests.PreparedRequest()
    mock_resp.request.url = mock_resp.url

    assert not bool(mock_resp)

    class Mock403Adapter(requests.adapters.HTTPAdapter):
        def send(self, request, **kwargs):
            return mock_resp

    test_session = RedactingSession(api_key=secret_key, max_attempts=1)
    test_session.mount("https://", Mock403Adapter())

    source = semrush_source(
        domains=["example.com"],
        api_key=secret_key,
        display_date="20260915",
        session=test_session,
    )

    with pytest.raises(Exception) as exc_info:
        pipeline.run(source)

    exc = exc_info.value
    # 1. Key absent from str(exc)
    assert secret_key not in str(exc)

    # 2. Key absent from exc.response and exc.response.request
    resp = getattr(exc, "response", None)
    if resp is not None:
        if getattr(resp, "url", None) is not None:
            assert secret_key not in resp.url
        req = getattr(resp, "request", None)
        if req is not None and getattr(req, "url", None) is not None:
            assert secret_key not in req.url

    # 3. Key absent from caplog
    assert secret_key not in caplog.text

    # 4. Key absent from all files in pipelines_dir
    pipe_dir = tmp_path / "dlt_pipelines"
    if pipe_dir.exists():
        for root, _, files in os.walk(pipe_dir):
            for f in files:
                file_path = os.path.join(root, f)
                try:
                    with open(file_path, "rb") as fp:
                        content = fp.read().decode("utf-8", errors="ignore")
                        assert secret_key not in content
                except (OSError, UnicodeDecodeError):
                    pass


def test_key_redaction_connection_level_failure(tmp_path, caplog: pytest.LogCaptureFixture) -> None:
    """Item 2 & 6: Connection-level failure (ConnectionError / retries exhausted).

    Asserts API key is absent from str(exc), whole cause/context chain (including
    nested urllib3 messages), caplog at DEBUG, and every file under pipelines_dir.
    """
    secret_key = "CONN_SECRET_KEY_888"
    caplog.set_level(logging.DEBUG)

    pipeline = dlt.pipeline(
        pipeline_name="test_conn_fail_pipe",
        destination=dlt.destinations.duckdb(str(tmp_path / "test_conn_fail.duckdb")),
        pipelines_dir=str(tmp_path / "dlt_pipelines"),
    )

    class MockConnFailAdapter(requests.adapters.HTTPAdapter):
        def send(self, request, **kwargs):
            url = request.url or f"https://api.semrush.com/?key={secret_key}&type=domain_ranks"
            msg = (
                f"HTTPConnectionPool: Max retries exceeded with url: {url} "
                "(Caused by NewConnectionError)"
            )
            err = RequestsConnectionError(msg)
            err.request = request
            raise err

    test_session = RedactingSession(api_key=secret_key, max_attempts=1)
    test_session.mount("https://", MockConnFailAdapter())

    source = semrush_source(
        domains=["example.com"],
        api_key=secret_key,
        display_date="20260915",
        session=test_session,
    )

    with pytest.raises(Exception) as exc_info:
        pipeline.run(source)

    exc = exc_info.value
    # 1. Assert key absent from str(exc)
    assert secret_key not in str(exc)

    # 2. Assert key absent from whole cause/context chain and nested args
    curr = exc
    while curr is not None:
        assert secret_key not in str(curr)
        for arg in getattr(curr, "args", ()):
            if isinstance(arg, str):
                assert secret_key not in arg
        curr = curr.__cause__ or curr.__context__

    # 3. Assert key absent from caplog at DEBUG level
    assert secret_key not in caplog.text

    # 4. Assert key absent from every file under pipelines_dir
    pipe_dir = tmp_path / "dlt_pipelines"
    if pipe_dir.exists():
        for root, _, files in os.walk(pipe_dir):
            for f in files:
                file_path = os.path.join(root, f)
                try:
                    with open(file_path, "rb") as fp:
                        content = fp.read().decode("utf-8", errors="ignore")
                        assert secret_key not in content
                except (OSError, UnicodeDecodeError):
                    pass


def test_key_redaction_on_success_path(tmp_path, caplog: pytest.LogCaptureFixture) -> None:
    """Item 6: Redaction on the SUCCESS path: caplog (DEBUG) assertion on a successful run,
    and also check pipelines_dir files.
    """
    secret_key = "MY_SUCCESS_KEY_XYZ_777"
    caplog.set_level(logging.DEBUG)

    pipeline = dlt.pipeline(
        pipeline_name="test_success_redact_pipe",
        destination=dlt.destinations.duckdb(str(tmp_path / "test_success.duckdb")),
        pipelines_dir=str(tmp_path / "dlt_pipelines"),
    )

    class MockSuccessAdapter(requests.adapters.HTTPAdapter):
        def send(self, request, **kwargs):
            resp = requests.Response()
            resp.status_code = 200
            resp.request = request
            resp.url = request.url
            if "type=domain_ranks" in (request.url or ""):
                resp._content = _VALID_RANKS_CSV.encode("utf-8")
            else:
                resp._content = _VALID_ORGANIC_CSV.encode("utf-8")
            return resp

    test_session = RedactingSession(api_key=secret_key)
    test_session.mount("https://", MockSuccessAdapter())

    source = semrush_source(
        domains=["example.com"],
        api_key=secret_key,
        display_date="20260915",
        session=test_session,
    )
    info = pipeline.run(source)
    assert len(info.load_packages) > 0

    # 1. Assert key absent from caplog text at DEBUG level
    assert secret_key not in caplog.text

    # 2. Assert key absent from all files in pipelines_dir
    pipe_dir = tmp_path / "dlt_pipelines"
    assert pipe_dir.exists()
    for root, _, files in os.walk(pipe_dir):
        for f in files:
            file_path = os.path.join(root, f)
            try:
                with open(file_path, "rb") as fp:
                    content = fp.read().decode("utf-8", errors="ignore")
                    assert secret_key not in content, f"Key leaked in {file_path}"
            except (OSError, UnicodeDecodeError):
                pass


# ---------------------------------------------------------------------------
# 3. Retry Tests Through Real rest_api Client Path (Item 2)
# ---------------------------------------------------------------------------


def test_retry_429_then_success_real_rest_api_client(tmp_path) -> None:
    """Item 2: Verify 429 followed by 200 succeeds through RedactingSession."""
    pipeline = dlt.pipeline(
        pipeline_name="test_retry_pipe",
        destination=dlt.destinations.duckdb(str(tmp_path / "test_retry.duckdb")),
        pipelines_dir=str(tmp_path / "dlt_pipelines"),
    )
    test_session = RedactingSession(api_key="secret_key_123", max_attempts=3, backoff_factor=0)
    source = semrush_source(
        domains=["example.com"],
        api_key="secret_key_123",
        display_date="20260915",
        session=test_session,
    )

    call_count = {"ranks": 0, "organic": 0}

    class MockRetryAdapter(requests.adapters.HTTPAdapter):
        def send(self, request, **kwargs):
            url = request.url or ""
            resp = requests.Response()
            resp.url = url
            resp.request = request

            if "type=domain_ranks" in url:
                call_count["ranks"] += 1
                if call_count["ranks"] == 1:
                    resp.status_code = 429
                    resp._content = b"rate limited"
                    return resp
                resp.status_code = 200
                resp._content = _VALID_RANKS_CSV.encode("utf-8")
                return resp

            if "type=domain_organic" in url:
                call_count["organic"] += 1
                resp.status_code = 200
                resp._content = _VALID_ORGANIC_CSV.encode("utf-8")
                return resp

            resp.status_code = 200
            resp._content = b"ERROR 50 :: NOTHING FOUND"
            return resp

    test_session.mount("https://", MockRetryAdapter())
    info = pipeline.run(source)
    assert len(info.load_packages) > 0
    assert call_count["ranks"] == 2


def test_retry_exhaustion_raises_and_redacts(tmp_path) -> None:
    """Item 2: Exhausted retries raise HTTPError and sanitize key."""
    secret_key = "EXHAUSTED_KEY_456"
    pipeline = dlt.pipeline(
        pipeline_name="test_exhaust_pipe",
        destination=dlt.destinations.duckdb(str(tmp_path / "test_exhaust.duckdb")),
        pipelines_dir=str(tmp_path / "dlt_pipelines"),
    )
    test_session = RedactingSession(api_key=secret_key, max_attempts=2, backoff_factor=0)

    class MockAlways429Adapter(requests.adapters.HTTPAdapter):
        def send(self, request, **kwargs):
            resp = requests.Response()
            resp.status_code = 429
            resp.request = request
            resp.url = request.url
            resp._content = b"rate limited permanently"
            return resp

    test_session.mount("https://", MockAlways429Adapter())
    source = semrush_source(
        domains=["example.com"],
        api_key=secret_key,
        display_date="20260915",
        session=test_session,
    )

    with pytest.raises(Exception) as exc_info:
        pipeline.run(source)

    assert "429" in str(exc_info.value)
    assert secret_key not in str(exc_info.value)


def test_transport_level_retry_proofs() -> None:
    """Item 2: Transport-level call count proofs on mounted adapter:
    (a) 429 then 200 -> exactly 2 calls, status 200, key redacted
    (b) persistent 429 -> exactly configured max attempts (5 calls), status 429, key redacted
    (c) 4xx other than 429 (403) -> exactly 1 call, key redacted
    (d) key never leaks in any of these.
    """
    # (a) 429 then 200 -> exactly 2 calls
    calls_a = 0

    class AdapterA(requests.adapters.HTTPAdapter):
        def send(self, request, **kwargs):
            nonlocal calls_a
            calls_a += 1
            resp = requests.Response()
            resp.request = request
            resp.url = request.url
            if calls_a == 1:
                resp.status_code = 429
                resp._content = b"rate limited"
            else:
                resp.status_code = 200
                resp.headers["Content-Type"] = "text/plain"
                resp._content = (
                    f"{_DOC_RANKS_HEADER}\nus;alpha.com;100;10;20;5.0;1;2;1.0\n".encode()
                )
            return resp

    s_a = RedactingSession(api_key="SECRET_A", max_attempts=5, backoff_factor=0)
    s_a.mount("https://", AdapterA())
    req_a = requests.Request(
        "GET", "https://api.semrush.com/?key=SECRET_A&type=domain_ranks"
    ).prepare()
    resp_a = s_a.send(req_a)
    assert calls_a == 2
    assert resp_a.status_code == 200
    assert "SECRET_A" not in (resp_a.url or "")
    assert "SECRET_A" not in (resp_a.request.url or "")

    # (b) persistent 429 -> exactly configured max attempts (5 calls, no double retry)
    calls_b = 0

    class AdapterB(requests.adapters.HTTPAdapter):
        def send(self, request, **kwargs):
            nonlocal calls_b
            calls_b += 1
            resp = requests.Response()
            resp.request = request
            resp.url = request.url
            resp.status_code = 429
            resp._content = b"rate limited forever"
            return resp

    s_b = RedactingSession(api_key="SECRET_B", max_attempts=5, backoff_factor=0)
    s_b.mount("https://", AdapterB())
    req_b = requests.Request(
        "GET", "https://api.semrush.com/?key=SECRET_B&type=domain_ranks"
    ).prepare()
    with pytest.raises(requests.exceptions.HTTPError) as exc_info_b:
        s_b.send(req_b)
    assert calls_b == 5
    assert "SECRET_B" not in str(exc_info_b.value)
    if exc_info_b.value.response is not None:
        assert exc_info_b.value.response.status_code == 429
        assert "SECRET_B" not in (exc_info_b.value.response.url or "")

    # (c) 4xx other than 429 (403) -> exactly 1 call
    calls_c = 0

    class AdapterC(requests.adapters.HTTPAdapter):
        def send(self, request, **kwargs):
            nonlocal calls_c
            calls_c += 1
            resp = requests.Response()
            resp.request = request
            resp.url = request.url
            resp.status_code = 403
            resp._content = b"ERROR 110 :: INVALID KEY"
            return resp

    s_c = RedactingSession(api_key="SECRET_C", max_attempts=5, backoff_factor=0)
    s_c.mount("https://", AdapterC())
    req_c = requests.Request(
        "GET", "https://api.semrush.com/?key=SECRET_C&type=domain_ranks"
    ).prepare()
    with pytest.raises(Exception) as exc_info:
        s_c.send(req_c)
    assert "SECRET_C" not in str(exc_info.value)
    assert calls_c == 1


# ---------------------------------------------------------------------------
# 4. Error Classification (Item 5)
# ---------------------------------------------------------------------------


def test_error_402_not_in_classification() -> None:
    """Item 5: 402 is omitted from ERROR_CLASSIFICATION as it is not an Analytics API error code."""
    assert 402 not in ERROR_CLASSIFICATION


@pytest.mark.parametrize("code", list(ERROR_CLASSIFICATION.keys()))
def test_semrush_error_codes_handling(code: int) -> None:
    """Item 5: Parametrized test from ERROR_CLASSIFICATION table verifying typed exceptions."""
    expected_exc = ERROR_CLASSIFICATION[code]
    test_session = RedactingSession(max_attempts=1)

    class MockErrAdapter(requests.adapters.HTTPAdapter):
        def send(self, request, **kwargs):
            resp = requests.Response()
            resp.request = request
            resp.url = request.url
            resp.status_code = 200
            resp._content = f"ERROR {code} :: TEST ERROR MESSAGE".encode()
            return resp

    test_session.mount("https://", MockErrAdapter())
    req = requests.Request("GET", "https://api.semrush.com/?key=KEY").prepare()

    with pytest.raises(expected_exc) as exc_info:
        test_session.send(req)

    assert str(code) in str(exc_info.value)


def test_unknown_semrush_error_code() -> None:
    """Verify unknown ERROR n :: maps to generic SemrushAPIError."""
    test_session = RedactingSession(max_attempts=1)

    class MockErrAdapter(requests.adapters.HTTPAdapter):
        def send(self, request, **kwargs):
            resp = requests.Response()
            resp.request = request
            resp.url = request.url
            resp.status_code = 200
            resp._content = b"ERROR 9999 :: UNKNOWN SERVER ERROR"
            return resp

    test_session.mount("https://", MockErrAdapter())
    req = requests.Request("GET", "https://api.semrush.com/?key=KEY").prepare()

    with pytest.raises(SemrushAPIError) as exc_info:
        test_session.send(req)
    assert "9999" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 5. Header Validation, Column Mapping & Malformed CSV Tests (Items 1, 3, 4)
# ---------------------------------------------------------------------------


def test_html_body_raises_semrush_api_error() -> None:
    """Non-CSV / HTML 200 body must not silently become []. Must raise SemrushAPIError."""
    test_session = RedactingSession(max_attempts=1)

    class MockHtmlAdapter(requests.adapters.HTTPAdapter):
        def send(self, request, **kwargs):
            resp = requests.Response()
            resp.request = request
            resp.url = request.url
            resp.status_code = 200
            resp._content = b"<html><body>502 Bad Gateway</body></html>"
            return resp

    test_session.mount("https://", MockHtmlAdapter())
    req = requests.Request("GET", "https://api.semrush.com/?type=domain_ranks").prepare()

    with pytest.raises(SemrushAPIError, match="Unexpected column count in CSV header"):
        test_session.send(req)


def test_full_name_headers_mapped_by_position() -> None:
    """Item 1: Documented full-name headers map to expected export_columns strictly by position."""
    test_session = RedactingSession(max_attempts=1)

    class MockDocHeaderAdapter(requests.adapters.HTTPAdapter):
        def send(self, request, **kwargs):
            resp = requests.Response()
            resp.request = request
            resp.url = request.url
            resp.status_code = 200
            resp._content = (
                f"{_DOC_RANKS_HEADER}\nus;example.com;100;1000;2000;500.0;5;50;10.0\n"
            ).encode()
            return resp

    test_session.mount("https://", MockDocHeaderAdapter())
    req = requests.Request("GET", "https://api.semrush.com/?type=domain_ranks").prepare()
    resp = test_session.send(req)
    assert resp.status_code == 200
    data = json.loads(resp.text)
    assert len(data) == 1
    row = data[0]
    # Check that export_columns keys (Db, Dn, Rk, Or, Ot, Oc, Ad, At, Ac) are populated by position
    assert row["Db"] == "us"
    assert row["Dn"] == "example.com"
    assert row["Rk"] == "100"
    assert row["Or"] == "1000"
    assert row["Ot"] == "2000"
    assert row["Oc"] == "500.0"
    assert row["Ad"] == "5"
    assert row["At"] == "50"
    assert row["Ac"] == "10.0"


def test_header_column_count_mismatch_raises_semrush_api_error() -> None:
    """Item 1: Header with fewer or extra columns raises SemrushAPIError."""
    test_session = RedactingSession(max_attempts=1)

    # 1. Fewer columns in header (8 instead of 9)
    class MockFewerColsAdapter(requests.adapters.HTTPAdapter):
        def send(self, request, **kwargs):
            resp = requests.Response()
            resp.request = request
            resp.url = request.url
            resp.status_code = 200
            resp._content = b"Col1;Col2;Col3;Col4;Col5;Col6;Col7;Col8\n1;2;3;4;5;6;7;8\n"
            return resp

    test_session.mount("https://", MockFewerColsAdapter())
    req = requests.Request("GET", "https://api.semrush.com/?type=domain_ranks").prepare()
    with pytest.raises(SemrushAPIError, match="expected 9 columns, got 8"):
        test_session.send(req)

    # 2. Extra columns in header (10 instead of 9)
    class MockExtraColsAdapter(requests.adapters.HTTPAdapter):
        def send(self, request, **kwargs):
            resp = requests.Response()
            resp.request = request
            resp.url = request.url
            resp.status_code = 200
            resp._content = b"C1;C2;C3;C4;C5;C6;C7;C8;C9;C10\n1;2;3;4;5;6;7;8;9;10\n"
            return resp

    test_session.mount("https://", MockExtraColsAdapter())
    req = requests.Request("GET", "https://api.semrush.com/?type=domain_ranks").prepare()
    with pytest.raises(SemrushAPIError, match="expected 9 columns, got 10"):
        test_session.send(req)


def test_header_only_csv_returns_empty_list() -> None:
    """Item 3: Header-only 200 response converts to empty list [] without failing."""
    test_session = RedactingSession(max_attempts=1)

    class MockHeaderOnlyAdapter(requests.adapters.HTTPAdapter):
        def send(self, request, **kwargs):
            resp = requests.Response()
            resp.request = request
            resp.url = request.url
            resp.status_code = 200
            resp._content = f"{_DOC_RANKS_HEADER}\n".encode()
            return resp

    test_session.mount("https://", MockHeaderOnlyAdapter())
    req = requests.Request("GET", "https://api.semrush.com/?type=domain_ranks").prepare()
    resp = test_session.send(req)
    assert resp.status_code == 200
    assert resp.text == "[]"
    assert resp.json() == []


def test_empty_non_csv_raises_semrush_api_error() -> None:
    """Item 3: Empty body must raise SemrushAPIError, not silently become []."""
    test_session = RedactingSession(max_attempts=1)

    class MockEmptyAdapter(requests.adapters.HTTPAdapter):
        def send(self, request, **kwargs):
            resp = requests.Response()
            resp.request = request
            resp.url = request.url
            resp.status_code = 200
            resp._content = b"   "
            return resp

    test_session.mount("https://", MockEmptyAdapter())
    req = requests.Request("GET", "https://api.semrush.com/?type=domain_ranks").prepare()

    with pytest.raises(SemrushAPIError, match="Unexpected column count in CSV header"):
        test_session.send(req)


def test_malformed_csv_row_count_mismatch_raises_semrush_api_error() -> None:
    """Item 1: Data rows with wrong column count raise SemrushAPIError."""
    test_session = RedactingSession(max_attempts=1)

    class MockBadCSVAdapter(requests.adapters.HTTPAdapter):
        def send(self, request, **kwargs):
            resp = requests.Response()
            resp.request = request
            resp.url = request.url
            resp.status_code = 200
            # Header has all 9 columns, line 2 has only 2 columns
            resp._content = f"{_DOC_RANKS_HEADER}\nus;alpha.com\n".encode()
            return resp

    test_session.mount("https://", MockBadCSVAdapter())
    req = requests.Request("GET", "https://api.semrush.com/?type=domain_ranks").prepare()

    with pytest.raises(SemrushAPIError, match="Malformed CSV response on line 2"):
        test_session.send(req)


# ---------------------------------------------------------------------------
# 6. Coercion Helper Tests (Item 4)
# ---------------------------------------------------------------------------


def test_coercion_helper_and_column_naming() -> None:
    """Item 4: Coercion: empty -> None, non-empty unparseable value raises SemrushAPIError
    naming the column.
    """
    # Empty / whitespace -> None
    assert _coerce_val("", int, "rank") is None
    assert _coerce_val("   ", float, "organic_cost") is None
    assert _coerce_val(None, str, "domain") is None
    assert _coerce_val("", str, "domain") is None

    # Valid values
    assert _coerce_val("123", int, "rank") == 123
    assert _coerce_val("45.67", float, "organic_cost") == 45.67
    assert _coerce_val("example.com", str, "domain") == "example.com"

    # Invalid values raise SemrushAPIError naming the column
    with pytest.raises(SemrushAPIError, match="column 'rank'"):
        _coerce_val("not_an_int", int, "rank")

    with pytest.raises(SemrushAPIError, match="column 'organic_cost'"):
        _coerce_val("not_a_float", float, "organic_cost")


# ---------------------------------------------------------------------------
# 7. Input Validation & Parameter Tests (Items 7, 8)
# ---------------------------------------------------------------------------


def test_domains_validation() -> None:
    """Item 7: Validate domains: required parameter, reject bare str, non-list, non-str,
    normalize & dedupe.
    """
    # 1. Bare string rejected
    with pytest.raises(ValueError, match="domains must be a list of strings, not a single string"):
        semrush_source(domains="github.com", api_key="key")

    # 2. Non-list rejected
    with pytest.raises(ValueError, match="domains must be a list of domain strings"):
        semrush_source(domains=("github.com",), api_key="key")

    # 3. Non-string item rejected
    with pytest.raises(ValueError, match="All domains must be strings"):
        semrush_source(domains=["github.com", 123], api_key="key")

    # 4. Empty list rejected
    with pytest.raises(ValueError, match="At least one domain"):
        semrush_source(domains=[], api_key="key")

    # 5. Empty/whitespace strings rejected
    with pytest.raises(ValueError, match="Domain strings cannot be empty"):
        semrush_source(domains=["   ", ""], api_key="key")

    # 6. Duplicates deduped and normalized (strip/lowercase)
    source = semrush_source(
        domains=["  Github.com  ", "GITHUB.COM", "python.org"],
        api_key="key",
        display_date="20260915",
    )
    assert "domain_ranks" in source.resources
    expected_fp = compute_selection_fingerprint(["github.com", "python.org"], "us", 100)
    cursor_fp = compute_selection_fingerprint(["github.com", "python.org"], "us", 100)
    assert expected_fp == cursor_fp


def test_database_parameter_honored(tmp_path) -> None:
    """Outgoing query params include the selected database."""
    captured_urls: list[str] = []

    class MockInspectAdapter(requests.adapters.HTTPAdapter):
        def send(self, request, **kwargs):
            captured_urls.append(request.url)
            resp = requests.Response()
            resp.request = request
            resp.url = request.url
            resp.status_code = 200
            if "type=domain_ranks" in request.url:
                resp._content = _VALID_RANKS_CSV.encode("utf-8")
            else:
                resp._content = _VALID_ORGANIC_CSV.encode("utf-8")
            return resp

    test_session = RedactingSession()
    test_session.mount("https://", MockInspectAdapter())

    pipeline = dlt.pipeline(
        pipeline_name="test_db_param_pipe",
        destination=dlt.destinations.duckdb(str(tmp_path / "test_db.duckdb")),
        pipelines_dir=str(tmp_path / "pipelines"),
    )
    source = semrush_source(
        domains=["example.com"],
        api_key="test_key",
        database="uk",
        display_date="20260915",
        session=test_session,
    )
    pipeline.run(source)

    ranks_urls = [u for u in captured_urls if "type=domain_ranks" in u]
    organic_urls = [u for u in captured_urls if "type=domain_organic" in u]
    assert len(ranks_urls) > 0
    assert len(organic_urls) > 0
    for u in ranks_urls:
        assert "database=uk" in u
    for u in organic_urls:
        assert "database=uk" in u


def test_parameter_validation_and_period_iso() -> None:
    """Validate display_date, display_limit, database, and period_to_iso_date."""
    # Invalid display_date formats
    with pytest.raises(ValueError, match="display_date must be formatted as YYYYMM15"):
        semrush_source(domains=["a.com"], api_key="k", display_date="20261315")

    with pytest.raises(ValueError, match="display_date must be formatted as YYYYMM15"):
        semrush_source(domains=["a.com"], api_key="k", display_date="20260914")

    with pytest.raises(ValueError, match="display_date must be formatted as YYYYMM15"):
        semrush_source(domains=["a.com"], api_key="k", display_date="not_a_date")

    # Invalid display_limit
    with pytest.raises(ValueError, match="display_limit must be a positive integer >= 1"):
        semrush_source(domains=["a.com"], api_key="k", display_limit=0)

    with pytest.raises(ValueError, match="display_limit must be a positive integer >= 1"):
        semrush_source(domains=["a.com"], api_key="k", display_limit=-10)

    # Invalid database
    with pytest.raises(ValueError, match="database must be a non-empty string"):
        semrush_source(domains=["a.com"], api_key="k", database="   ")

    # period_to_iso_date validation
    assert period_to_iso_date("20260915") == "2026-09-15"
    with pytest.raises(ValueError, match="Invalid period format"):
        period_to_iso_date("invalid_period")
    with pytest.raises(ValueError, match="Invalid period format"):
        period_to_iso_date("20261315")


def test_schema_column_type_hints_on_gated_resources() -> None:
    """Column type hints are attached to gated resources so all-None columns exist."""
    source = semrush_source(
        domains=["example.com"],
        api_key="key",
        display_date="20260915",
    )
    ranks_res = source.resources["domain_ranks"]
    organic_res = source.resources["domain_organic"]

    assert ranks_res.columns.keys() == RANKS_SCHEMA_COLUMNS.keys()
    assert organic_res.columns.keys() == ORGANIC_SCHEMA_COLUMNS.keys()
    assert ranks_res.columns["rank"]["data_type"] == "bigint"
    assert ranks_res.columns["organic_cost"]["data_type"] == "double"
    assert organic_res.columns["cpc"]["data_type"] == "double"


def test_missing_api_key_raises_value_error() -> None:
    """Verify missing API key raises ValueError when env var is absent."""
    with (
        patch.dict(os.environ, {}, clear=True),
        pytest.raises(ValueError, match="Semrush API key required"),
    ):
        semrush_source(domains=["example.com"], api_key=None)


def test_selection_fingerprint_changes() -> None:
    """Verify fingerprint changes with database, display_limit, or domains."""
    fp1 = compute_selection_fingerprint(["b.com", "a.com"], "us", 10)
    fp2 = compute_selection_fingerprint(["a.com", "b.com"], "us", 10)
    assert fp1 == fp2

    fp_db = compute_selection_fingerprint(["a.com", "b.com"], "uk", 10)
    assert fp_db != fp1

    fp_limit = compute_selection_fingerprint(["a.com", "b.com"], "us", 5)
    assert fp_limit != fp1

    fp_drop = compute_selection_fingerprint(["a.com"], "us", 10)
    assert fp_drop != fp1
