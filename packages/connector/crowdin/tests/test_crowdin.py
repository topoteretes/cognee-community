"""Unit tests for the Crowdin data-source connector.

All tests run completely offline without live server access or network requests,
using synthetic localization fixtures and mocked HTTP transports.
"""

from __future__ import annotations

import httpx
import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

from cognee_community_connector_crowdin.crowdin import (
    CROWDIN_SOURCE_NAME,
    CROWDIN_TABLE_NAME,
    CrowdinAuthError,
    CrowdinClient,
    CrowdinConfig,
    CrowdinRateLimitError,
    _build_glossary_markdown,
    _build_project_overview_markdown,
    _build_strings_markdown,
    _redact_token,
    crowdin_connector,
    crowdin_source,
)

# ---------------------------------------------------------------------------
# Synthetic Test Fixtures
# ---------------------------------------------------------------------------

SAMPLE_PROJECTS = {
    "data": [
        {
            "data": {
                "id": 101,
                "name": "Acme Core Platform",
                "identifier": "acme-core",
                "sourceLanguageId": "en",
                "targetLanguageIds": ["de", "fr", "ja"],
                "description": "Primary user interface and authentication flows.",
                "webUrl": "https://crowdin.com/project/acme-core",
            }
        },
        {
            "data": {
                "id": 102,
                "name": "Acme Mobile App",
                "identifier": "acme-mobile",
                "sourceLanguageId": "en",
                "targetLanguageIds": ["es", "pt-BR"],
                "description": "iOS and Android client strings.",
                "webUrl": "https://crowdin.com/project/acme-mobile",
            }
        },
    ]
}

SAMPLE_GLOSSARIES = {
    "data": [
        {
            "data": {
                "id": 501,
                "name": "Acme Technical Lexicon",
                "text": "Approved terminology across all products.",
            }
        }
    ]
}

SAMPLE_TERMS = {
    "data": [
        {
            "data": {
                "id": 9001,
                "text": "Passkey",
                "languageId": "en",
                "partOfSpeech": "noun",
                "status": "approved",
                "description": "FIDO2 WebAuthn cryptographic credential.",
            }
        },
        {
            "data": {
                "id": 9002,
                "text": "Passkey",
                "languageId": "de",
                "partOfSpeech": "noun",
                "status": "approved",
                "description": "Do not translate as Passwort. Keep as Passkey.",
            }
        },
    ]
}

SAMPLE_STRINGS = {
    "data": [
        {
            "data": {
                "id": 801,
                "identifier": "auth.login.button",
                "text": "Sign in with your Passkey",
                "context": "Main login submission button on web.",
            }
        }
    ]
}

SAMPLE_TRANSLATIONS = {
    "data": [
        {
            "data": {
                "stringId": 801,
                "languageId": "de",
                "text": "Mit Passkey anmelden",
                "rating": 10,
            }
        },
        {
            "data": {
                "stringId": 801,
                "languageId": "fr",
                "text": "Se connecter avec votre Passkey",
                "rating": 9,
            }
        },
    ]
}


class MockTransport(httpx.BaseTransport):
    """Mock HTTP transport returning synthetic Crowdin API responses."""

    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.call_count = 0
        self.last_request = None

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.call_count += 1
        self.last_request = request

        if self.status_code != 200:
            return httpx.Response(status_code=self.status_code, text="Simulated Crowdin error")

        path = request.url.path
        if "/projects" in path and "/strings" in path:
            return httpx.Response(200, json=SAMPLE_STRINGS)
        if "/projects" in path and "/translations" in path:
            return httpx.Response(200, json=SAMPLE_TRANSLATIONS)
        if "/projects" in path:
            return httpx.Response(200, json=SAMPLE_PROJECTS)
        if "/glossaries/" in path and "/terms" in path:
            return httpx.Response(200, json=SAMPLE_TERMS)
        if "/glossaries" in path:
            return httpx.Response(200, json=SAMPLE_GLOSSARIES)

        return httpx.Response(200, json={"data": []})


# ---------------------------------------------------------------------------
# Helper & Redaction Tests
# ---------------------------------------------------------------------------


def test_redact_token() -> None:
    token = "crowdin_pat_secret_abc123"
    msg = f"Request rejected for token {token} on endpoint"
    redacted = _redact_token(msg, token)

    assert token not in redacted
    assert "[REDACTED_CROWDIN_TOKEN]" in redacted


def test_client_enterprise_url() -> None:
    client = CrowdinClient(api_token="test", organization_domain="acme-corp")
    assert "https://acme-corp.api.crowdin.com/api/v2" == client.base_url


# ---------------------------------------------------------------------------
# Client Tests
# ---------------------------------------------------------------------------


def test_client_auth_error_401() -> None:
    transport = MockTransport(status_code=401)
    client = httpx.Client(transport=transport)
    crowdin_client = CrowdinClient("invalid_pat", client=client)

    with pytest.raises(CrowdinAuthError) as exc_info:
        crowdin_client.get_projects()

    assert "authentication rejected" in str(exc_info.value)


def test_client_rate_limit_error_429() -> None:
    transport = MockTransport(status_code=429)
    client = httpx.Client(transport=transport)
    crowdin_client = CrowdinClient("valid_pat", client=client)

    with pytest.raises(CrowdinRateLimitError):
        crowdin_client.get_projects()


def test_client_get_projects() -> None:
    transport = MockTransport()
    client = httpx.Client(transport=transport)
    crowdin_client = CrowdinClient("valid_pat", client=client)

    projects = crowdin_client.get_projects()
    assert len(projects) == 2
    assert projects[0]["identifier"] == "acme-core"
    assert projects[0]["sourceLanguageId"] == "en"


def test_client_get_glossaries_and_terms() -> None:
    transport = MockTransport()
    client = httpx.Client(transport=transport)
    crowdin_client = CrowdinClient("valid_pat", client=client)

    glossaries = crowdin_client.get_glossaries()
    assert len(glossaries) == 1
    assert glossaries[0]["name"] == "Acme Technical Lexicon"

    terms = crowdin_client.get_glossary_terms(501)
    assert len(terms) == 2
    assert terms[0]["text"] == "Passkey"


def test_client_get_strings_and_translations() -> None:
    transport = MockTransport()
    client = httpx.Client(transport=transport)
    crowdin_client = CrowdinClient("valid_pat", client=client)

    strings = crowdin_client.get_source_strings(101)
    assert len(strings) == 1
    assert strings[0]["identifier"] == "auth.login.button"

    translations = crowdin_client.get_string_translations(101, 801)
    assert len(translations) == 2
    assert translations[0]["languageId"] == "de"
    assert translations[0]["text"] == "Mit Passkey anmelden"


# ---------------------------------------------------------------------------
# Markdown Formatting Tests
# ---------------------------------------------------------------------------


def test_build_project_overview_markdown() -> None:
    project = SAMPLE_PROJECTS["data"][0]["data"]
    md = _build_project_overview_markdown(project)

    assert "# Crowdin Project: Acme Core Platform" in md
    assert "Identifier**: `acme-core`" in md
    assert "Source Language**: `en`" in md
    assert "de, fr, ja" in md


def test_build_glossary_markdown() -> None:
    glossary = SAMPLE_GLOSSARIES["data"][0]["data"]
    terms = [item["data"] for item in SAMPLE_TERMS["data"]]
    md = _build_glossary_markdown(glossary, terms)

    assert "# Terminology Glossary: Acme Technical Lexicon" in md
    assert "### Term: `Passkey` (EN)" in md
    assert "### Term: `Passkey` (DE)" in md
    assert "FIDO2 WebAuthn cryptographic credential." in md


def test_build_strings_markdown() -> None:
    string_entry = dict(SAMPLE_STRINGS["data"][0]["data"])
    string_entry["translations"] = [item["data"] for item in SAMPLE_TRANSLATIONS["data"]]
    md = _build_strings_markdown("Acme Core Platform", 101, [string_entry])

    assert "# Localization Strings & Translations: Acme Core Platform" in md
    assert "Key: `auth.login.button`" in md
    assert "Sign in with your Passkey" in md
    assert '`[DE]`: "Mit Passkey anmelden"' in md
    assert '`[FR]`: "Se connecter avec votre Passkey"' in md


# ---------------------------------------------------------------------------
# DLT Source Invariant & Integration Tests
# ---------------------------------------------------------------------------


def test_crowdin_source_document_mode_invariant() -> None:
    transport = MockTransport()
    fake_client = CrowdinClient("test_pat", client=httpx.Client(transport=transport))

    source = crowdin_source(api_token="test_pat", client=fake_client)

    # Invariant: Must declare DOCUMENT_SOURCE_ATTR = "crowdin"
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == CROWDIN_SOURCE_NAME


def test_crowdin_source_yields_expected_reports() -> None:
    transport = MockTransport()
    fake_client = CrowdinClient("test_pat", client=httpx.Client(transport=transport))

    source = crowdin_source(
        api_token="test_pat",
        include_glossaries=True,
        include_strings=True,
        client=fake_client,
    )

    resources = list(source.selected_resources.values())
    assert len(resources) == 1
    resource = resources[0]
    assert resource.name == CROWDIN_TABLE_NAME
    assert resource.write_disposition == "replace"

    rows = list(resource())
    # 2 project overviews + 1 glossary + 2 string catalogs (for 2 projects) = 5
    assert len(rows) == 5

    report_types = [r["metadata"]["report_type"] for r in rows]
    assert "project_overview" in report_types
    assert "glossary" in report_types
    assert "strings_and_translations" in report_types

    for r in rows:
        assert r["metadata"]["source"] == CROWDIN_SOURCE_NAME
        assert len(r["id"]) > 0
        assert r["text"].startswith("# ")


def test_crowdin_source_deterministic_id_stability() -> None:
    transport = MockTransport()
    fake_client = CrowdinClient("test_pat", client=httpx.Client(transport=transport))

    source_1 = crowdin_source(api_token="test_pat", client=fake_client)
    rows_1 = list(next(iter(source_1.selected_resources.values()))())

    source_2 = crowdin_source(api_token="test_pat", client=fake_client)
    rows_2 = list(next(iter(source_2.selected_resources.values()))())

    # Deterministic IDs ensure duplicate runs don't re-cognify identical reports
    assert [r["id"] for r in rows_1] == [r["id"] for r in rows_2]


def test_crowdin_source_filters_by_project_ids() -> None:
    transport = MockTransport()
    fake_client = CrowdinClient("test_pat", client=httpx.Client(transport=transport))

    source = crowdin_source(
        api_token="test_pat",
        project_ids=[101],
        client=fake_client,
    )

    resource = next(iter(source.selected_resources.values()))
    rows = list(resource())

    proj_rows = [r for r in rows if r["metadata"]["report_type"] == "project_overview"]
    assert len(proj_rows) == 1
    assert proj_rows[0]["metadata"]["project_id"] == 101


@pytest.mark.asyncio
async def test_crowdin_connector_async_wrapper() -> None:
    transport = MockTransport()
    fake_client = CrowdinClient("test_pat", client=httpx.Client(transport=transport))

    config = CrowdinConfig(
        api_token="test_pat",
        project_ids=[101],
    )

    documents = await crowdin_connector(config, client=fake_client)
    assert len(documents) == 3  # 1 overview + 1 glossary + 1 strings
    for doc in documents:
        assert "id" in doc
        assert "text" in doc
        assert doc["metadata"]["source"] == "crowdin"
