"""freshdesk_source() factory, settings and the small client helpers."""

import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR
from fake_freshdesk import FakeFreshdesk

from cognee_community_connector_freshdesk import (
    FreshdeskClient,
    freshdesk_source,
    normalize_domain,
)
from cognee_community_connector_freshdesk.freshdesk import _header_int, _next_link

ENV = ("FRESHDESK_DOMAIN", "FRESHDESK_API_KEY")


@pytest.fixture(autouse=True)
def no_freshdesk_env(monkeypatch):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)


def fake_client():
    return FreshdeskClient("acme", "k", session=FakeFreshdesk(), sleep=lambda _s: None)


# ---------------------------------------------------------------------------
# freshdesk_source()
# ---------------------------------------------------------------------------
def test_resource_opts_into_document_mode_and_pipeline_scope():
    resource = freshdesk_source(client=fake_client(), resource_name="acme_support")

    assert resource.name == "acme_support"
    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == "freshdesk"
    assert getattr(resource, PIPELINE_SCOPE_ATTR) == "acme_support"
    assert resource.cognee_sync_stats == {}


def test_missing_credentials_fail_when_the_source_is_built():
    with pytest.raises(ValueError, match="credentials are missing"):
        freshdesk_source()

    with pytest.raises(ValueError, match="credentials are missing"):
        freshdesk_source(domain="acme")


def test_credentials_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("FRESHDESK_DOMAIN", "acme")
    monkeypatch.setenv("FRESHDESK_API_KEY", "fwapi_test")

    freshdesk_source()  # builds without contacting Freshdesk


def test_selecting_nothing_is_refused_instead_of_forgetting_everything():
    with pytest.raises(ValueError, match="Select something to sync"):
        freshdesk_source(client=fake_client(), include_tickets=False, include_articles=False)


# ---------------------------------------------------------------------------
# Client helpers
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value",
    [
        "acme",
        "ACME",
        " acme.freshdesk.com ",
        "https://acme.freshdesk.com/",
        "https://acme.freshdesk.com/a/tickets/4",
    ],
)
def test_domain_is_normalized_to_the_helpdesk_host(value):
    assert normalize_domain(value) == "acme.freshdesk.com"


def test_empty_domain_is_a_clear_error():
    with pytest.raises(ValueError, match="domain is empty"):
        normalize_domain("  ")


@pytest.mark.parametrize(
    ("raw", "expected"), [("48", 48), ("38.0", 38), ("", None), (None, None), ("x", None)]
)
def test_rate_limit_headers_parse_both_integer_and_float_forms(raw, expected):
    assert _header_int({"X-Ratelimit-Remaining": raw}, "X-Ratelimit-Remaining") == expected


def test_next_link_is_read_from_the_link_header():
    link = '<https://acme.freshdesk.com/api/v2/tickets?per_page=2&page=3>; rel="next"'

    assert _next_link(link) == "https://acme.freshdesk.com/api/v2/tickets?per_page=2&page=3"
    assert _next_link(None) is None
    assert _next_link('<https://x>; rel="prev"') is None
