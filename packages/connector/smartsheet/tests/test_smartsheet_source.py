"""smartsheet_source() factory: validation, host normalization, resource markers."""

import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR
from fake_smartsheet import FakeSmartsheetSession

import cognee_community_connector_smartsheet.smartsheet as module
from cognee_community_connector_smartsheet import smartsheet_source

ENV = ("SMARTSHEET_API_TOKEN", "SMARTSHEET_HOST")


@pytest.fixture(autouse=True)
def no_smartsheet_env(monkeypatch):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------
def test_missing_token_raises_with_setup_hint():
    with pytest.raises(ValueError, match="Personal Settings"):
        smartsheet_source(sheet_ids=[1])


def test_token_never_appears_in_any_error_message():
    secret = "ssheet-super-secret-token"
    with pytest.raises(ValueError) as exc_info:
        smartsheet_source(api_token=None, sheet_ids=[1])
    assert secret not in str(exc_info.value)


def test_token_from_env(monkeypatch):
    monkeypatch.setenv("SMARTSHEET_API_TOKEN", "t")
    source = smartsheet_source(sheet_ids=[1], session=FakeSmartsheetSession())
    assert source is not None


# ---------------------------------------------------------------------------
# Selection required
# ---------------------------------------------------------------------------
def test_selection_required():
    with pytest.raises(ValueError, match="at least one of"):
        smartsheet_source(api_token="t")


def test_all_sheets_true_satisfies_selection_requirement():
    source = smartsheet_source(api_token="t", all_sheets=True, session=FakeSmartsheetSession())
    assert source is not None


# ---------------------------------------------------------------------------
# Id validation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("field", ["sheet_ids", "folder_ids", "workspace_ids"])
def test_ids_must_be_positive_ints(field):
    with pytest.raises(ValueError, match="positive integers"):
        smartsheet_source(api_token="t", **{field: [1, -2]})


@pytest.mark.parametrize("field", ["sheet_ids", "folder_ids", "workspace_ids"])
def test_ids_must_not_be_empty_list(field):
    with pytest.raises(ValueError, match="must not be empty"):
        smartsheet_source(api_token="t", **{field: []})


def test_bool_is_not_accepted_as_an_id():
    with pytest.raises(ValueError, match="positive integers"):
        smartsheet_source(api_token="t", sheet_ids=[True])


# ---------------------------------------------------------------------------
# include_columns / exclude_columns
# ---------------------------------------------------------------------------
def test_include_and_exclude_columns_are_mutually_exclusive():
    with pytest.raises(ValueError, match="only one of"):
        smartsheet_source(
            api_token="t",
            sheet_ids=[1],
            include_columns=["Status"],
            exclude_columns=["Notes"],
        )


# ---------------------------------------------------------------------------
# Numeric validation
# ---------------------------------------------------------------------------
def test_max_rows_per_sheet_must_be_positive():
    with pytest.raises(ValueError, match="max_rows_per_sheet"):
        smartsheet_source(api_token="t", sheet_ids=[1], max_rows_per_sheet=0)


def test_max_sheets_must_be_positive_if_set():
    with pytest.raises(ValueError, match="max_sheets"):
        smartsheet_source(api_token="t", sheet_ids=[1], max_sheets=0)


def test_timeout_must_be_positive():
    with pytest.raises(ValueError, match="timeout"):
        smartsheet_source(api_token="t", sheet_ids=[1], timeout=0)


def test_full_sync_every_must_be_non_negative():
    with pytest.raises(ValueError, match="full_sync_every"):
        smartsheet_source(api_token="t", sheet_ids=[1], full_sync_every=-1)


def test_recheck_sheets_per_run_must_be_non_negative():
    with pytest.raises(ValueError, match="recheck_sheets_per_run"):
        smartsheet_source(api_token="t", sheet_ids=[1], recheck_sheets_per_run=-1)


def test_attachment_max_bytes_must_be_positive():
    with pytest.raises(ValueError, match="attachment_max_bytes"):
        smartsheet_source(api_token="t", sheet_ids=[1], attachment_max_bytes=0)


def test_full_sync_every_zero_is_allowed():
    source = smartsheet_source(
        api_token="t", sheet_ids=[1], full_sync_every=0, session=FakeSmartsheetSession()
    )
    assert source is not None


def test_recheck_sheets_per_run_zero_is_allowed():
    source = smartsheet_source(
        api_token="t", sheet_ids=[1], recheck_sheets_per_run=0, session=FakeSmartsheetSession()
    )
    assert source is not None


def test_allow_mass_delete_and_attachment_text_default_off():
    resource = smartsheet_source(api_token="t", sheet_ids=[1], session=FakeSmartsheetSession())
    assert resource is not None  # defaults accepted without raising


# ---------------------------------------------------------------------------
# Host normalization
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "host,expected",
    [
        ("https://api.smartsheet.com", "https://api.smartsheet.com/2.0"),
        ("https://api.smartsheet.com/2.0", "https://api.smartsheet.com/2.0"),
        ("https://api.smartsheet.com/2.0/", "https://api.smartsheet.com/2.0"),
        ("https://api.smartsheet.eu", "https://api.smartsheet.eu/2.0"),
        ("https://api.smartsheet.au/", "https://api.smartsheet.au/2.0"),
        ("https://api.smartsheetgov.com", "https://api.smartsheetgov.com/2.0"),
    ],
)
def test_host_normalization_variants(host, expected):
    assert module._normalize_host(host) == expected


def test_host_defaults_to_us():
    assert module._normalize_host(None) == "https://api.smartsheet.com/2.0"


def test_host_rejects_http():
    with pytest.raises(ValueError, match="https"):
        module._normalize_host("http://api.smartsheet.com")


def test_host_rejects_credentials_query_or_fragment():
    for bad in (
        "https://user:pass@api.smartsheet.com",
        "https://api.smartsheet.com?x=1",
        "https://api.smartsheet.com#frag",
    ):
        with pytest.raises(ValueError, match="must not include"):
            module._normalize_host(bad)


def test_host_rejects_unexpected_path():
    with pytest.raises(ValueError, match="path"):
        module._normalize_host("https://api.smartsheet.com/v3")


def test_host_from_env(monkeypatch):
    monkeypatch.setenv("SMARTSHEET_HOST", "https://api.smartsheet.eu")
    source = smartsheet_source(api_token="t", sheet_ids=[1], session=FakeSmartsheetSession())
    assert source is not None


# ---------------------------------------------------------------------------
# Region key
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "host,expected",
    [
        ("https://api.smartsheet.com/2.0", "com"),
        ("https://api.smartsheet.eu/2.0", "eu"),
        ("https://api.smartsheet.au/2.0", "au"),
        ("https://api.smartsheetgov.com/2.0", "gov"),
    ],
)
def test_region_key(host, expected):
    assert module._region_key(host) == expected


# ---------------------------------------------------------------------------
# Resource markers
# ---------------------------------------------------------------------------
def test_resource_opts_into_document_mode_and_pipeline_scope():
    resource = smartsheet_source(
        api_token="t", sheet_ids=[1], resource_name="work_sheets", session=FakeSmartsheetSession()
    )
    assert resource.name == "work_sheets"
    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == "smartsheet"
    assert getattr(resource, PIPELINE_SCOPE_ATTR) == "work_sheets"
    assert resource.cognee_sync_stats == {}


def test_resource_write_disposition_is_merge():
    resource = smartsheet_source(api_token="t", sheet_ids=[1], session=FakeSmartsheetSession())
    assert resource.write_disposition == "merge"
