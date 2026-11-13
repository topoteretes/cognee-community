"""Hermetic unit test suite for the Cognee Packagist / Composer Connector."""

import json

import httpx
import pytest

from cognee_community_connector_packagist.packagist import (
    PackagistClient,
    PackagistConnectorError,
    PackagistRenderOptions,
    extract_packages_from_composer_file,
    packagist_source,
    render_packagist_document,
    sync_packagist_records,
)

# Mock Packagist API fixtures
MOCK_PKG_MONOLOG = {
    "package": {
        "name": "monolog/monolog",
        "description": "Sends your logs to files, sockets, and various web services",
        "time": "2026-10-01T12:00:00+00:00",
        "maintainers": [{"name": "Seldaek"}],
        "versions": {
            "3.5.0": {
                "name": "monolog/monolog",
                "version": "3.5.0",
                "license": ["MIT"],
                "require": {"php": ">=8.1", "psr/log": "^3.0"},
            },
            "3.4.0": {
                "name": "monolog/monolog",
                "version": "3.4.0",
                "license": ["MIT"],
                "require": {"php": ">=8.1"},
            },
        },
        "type": "library",
        "repository": "https://github.com/Seldaek/monolog",
        "github_stars": 21000,
        "downloads": {"total": 500000000},
        "abandoned": False,
    }
}

MOCK_PKG_SWIFTMAILER = {
    "package": {
        "name": "swiftmailer/swiftmailer",
        "description": "Free feature-rich PHP mailer",
        "repository": "https://github.com/swiftmailer/swiftmailer",
        "github_stars": 4000,
        "downloads": {"total": 120000000},
        "abandoned": "symfony/mailer",
        "versions": {
            "v6.3.0": {
                "name": "swiftmailer/swiftmailer",
                "version": "v6.3.0",
                "license": ["MIT"],
                "require": {"php": ">=7.0.0"},
            }
        },
    }
}

MOCK_ADVISORIES = {
    "advisories": {
        "monolog/monolog": [
            {
                "advisoryId": "PKSA-dmw8-jd8k-q3c6",
                "title": "Header Injection in NativeMailerHandler",
                "cve": "CVE-2024-1234",
                "link": "https://github.com/Seldaek/monolog/security/advisories/GHSA-123",
                "affectedVersions": "<2.8.0",
                "reportedAt": "2024-05-01T00:00:00+00:00",
            }
        ]
    }
}

MOCK_VENDOR_LIST = {
    "packageNames": [
        "guzzlehttp/guzzle",
        "guzzlehttp/psr7",
        "guzzlehttp/promises",
    ]
}

MOCK_CHANGES = {
    "timestamp": 17934536320011,
    "actions": [{"type": "update", "package": "monolog/monolog", "time": 17934536320000}],
}


class FakePackagistTransport(httpx.BaseTransport):
    """Hermetic mock HTTP transport simulating Packagist REST endpoints."""

    def __init__(self, simulate_rate_limit: bool = False):
        self.simulate_rate_limit = simulate_rate_limit
        self.rate_limit_counter = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)

        if self.simulate_rate_limit and self.rate_limit_counter == 0:
            self.rate_limit_counter += 1
            return httpx.Response(status_code=429, headers={"Retry-After": "0"})

        if "packages/monolog/monolog.json" in url_str:
            return httpx.Response(status_code=200, json=MOCK_PKG_MONOLOG)

        if "packages/swiftmailer/swiftmailer.json" in url_str:
            return httpx.Response(status_code=200, json=MOCK_PKG_SWIFTMAILER)

        if "api/security-advisories" in url_str:
            return httpx.Response(status_code=200, json=MOCK_ADVISORIES)

        if "packages/list.json" in url_str:
            return httpx.Response(status_code=200, json=MOCK_VENDOR_LIST)

        if "metadata/changes.json" in url_str:
            return httpx.Response(status_code=200, json=MOCK_CHANGES)

        if "packages/nonexistent/package.json" in url_str:
            return httpx.Response(
                status_code=404, json={"status": "error", "message": "Package not found"}
            )

        return httpx.Response(status_code=404, json={"error": "Not Found"})


def create_mock_client(simulate_rate_limit: bool = False) -> PackagistClient:
    transport = FakePackagistTransport(simulate_rate_limit=simulate_rate_limit)
    http_client = httpx.Client(transport=transport)
    return PackagistClient(client=http_client)


# ─── Tests ────────────────────────────────────────────────────────────────────


def test_client_headers():
    client = create_mock_client()
    headers = client._get_headers()
    assert "cognee-community-connector-packagist" in headers["User-Agent"]
    assert headers["Accept"] == "application/json"


def test_get_package_metadata():
    client = create_mock_client()
    pkg = client.get_package("monolog/monolog")
    assert pkg["name"] == "monolog/monolog"
    assert pkg["github_stars"] == 21000
    assert pkg["abandoned"] is False


def test_get_package_not_found():
    client = create_mock_client()
    with pytest.raises(PackagistConnectorError) as exc_info:
        client.get_package("nonexistent/package")
    assert exc_info.value.status_code == 404


def test_rate_limit_backoff():
    client = create_mock_client(simulate_rate_limit=True)
    pkg = client.get_package("monolog/monolog")
    assert pkg["name"] == "monolog/monolog"


def test_get_security_advisories():
    client = create_mock_client()
    advisories = client.get_security_advisories(["monolog/monolog"])
    assert "monolog/monolog" in advisories
    assert advisories["monolog/monolog"][0]["advisoryId"] == "PKSA-dmw8-jd8k-q3c6"
    assert advisories["monolog/monolog"][0]["cve"] == "CVE-2024-1234"


def test_list_vendor_packages():
    client = create_mock_client()
    pkgs = client.list_vendor_packages("guzzlehttp")
    assert len(pkgs) == 3
    assert "guzzlehttp/guzzle" in pkgs


def test_get_changes_feed():
    client = create_mock_client()
    changes = client.get_changes()
    assert changes["timestamp"] == 17934536320011
    assert len(changes["actions"]) == 1


def test_render_packagist_document_active():
    pkg = MOCK_PKG_MONOLOG["package"]
    advisories = MOCK_ADVISORIES["advisories"]["monolog/monolog"]

    doc = render_packagist_document(pkg, advisories)

    assert doc["id"] == "packagist:monolog/monolog"
    assert doc["title"] == "Packagist: monolog/monolog"
    assert doc["_deleted"] is False
    assert doc["is_abandoned"] is False
    assert doc["replacement_package"] is None
    assert doc["advisory_count"] == 1

    # Verify content formatting
    assert "# Packagist Package: monolog/monolog" in doc["content"]
    assert "Status: Active / Maintained" in doc["content"]
    assert "PKSA-dmw8-jd8k-q3c6" in doc["content"]
    assert "CVE-2024-1234" in doc["content"]
    assert "PHP requirement: >=8.1" in doc["content"]


def test_render_packagist_document_abandoned_with_replacement():
    pkg = MOCK_PKG_SWIFTMAILER["package"]
    doc = render_packagist_document(pkg, advisories=[])

    assert doc["id"] == "packagist:swiftmailer/swiftmailer"
    assert doc["is_abandoned"] is True
    assert doc["replacement_package"] == "symfony/mailer"
    assert "⚠️ ABANDONED (Author recommends replacement: 'symfony/mailer')" in doc["content"]


def test_sync_packagist_records_normal():
    client = create_mock_client()
    docs = list(sync_packagist_records(client, ["monolog/monolog"]))
    assert len(docs) == 1
    assert docs[0]["id"] == "packagist:monolog/monolog"
    assert docs[0]["_deleted"] is False


def test_sync_packagist_records_tombstone_on_404():
    client = create_mock_client()
    docs = list(sync_packagist_records(client, ["nonexistent/package"]))
    assert len(docs) == 1
    assert docs[0]["id"] == "packagist:nonexistent/package"
    assert docs[0]["_deleted"] is True
    assert "Deleted" in docs[0]["title"]


def test_sync_packagist_records_exclude_abandoned():
    client = create_mock_client()
    options = PackagistRenderOptions(exclude_abandoned=True)
    docs = list(sync_packagist_records(client, ["swiftmailer/swiftmailer"], options=options))
    assert len(docs) == 1
    assert docs[0]["id"] == "packagist:swiftmailer/swiftmailer"
    assert docs[0]["_deleted"] is True
    assert "Abandoned" in docs[0]["title"]


def test_packagist_source_execution():
    source = packagist_source(
        packages=["monolog/monolog"],
        vendor="guzzlehttp",
    )
    assert source is not None


def test_extract_packages_from_composer_json(tmp_path):
    composer_file = tmp_path / "composer.json"
    composer_file.write_text(
        json.dumps(
            {
                "require": {
                    "php": ">=8.1",
                    "ext-json": "*",
                    "monolog/monolog": "^3.0",
                },
                "require-dev": {
                    "phpunit/phpunit": "^10.0",
                },
            }
        ),
        encoding="utf-8",
    )

    pkgs = extract_packages_from_composer_file(composer_file)
    assert pkgs == ["monolog/monolog", "phpunit/phpunit"]


def test_extract_packages_from_composer_lock(tmp_path):
    lock_file = tmp_path / "composer.lock"
    lock_file.write_text(
        json.dumps(
            {
                "packages": [
                    {"name": "guzzlehttp/guzzle", "version": "7.8.0"},
                    {"name": "psr/log", "version": "3.0.0"},
                ],
                "packages-dev": [
                    {"name": "mockery/mockery", "version": "1.6.0"},
                ],
            }
        ),
        encoding="utf-8",
    )

    pkgs = extract_packages_from_composer_file(lock_file)
    assert pkgs == ["guzzlehttp/guzzle", "mockery/mockery", "psr/log"]


def test_extract_packages_file_not_found():
    with pytest.raises(FileNotFoundError):
        extract_packages_from_composer_file("nonexistent_composer.json")


def test_packagist_source_with_composer_file(tmp_path):
    composer_file = tmp_path / "composer.json"
    composer_file.write_text(json.dumps({"require": {"monolog/monolog": "^3.0"}}), encoding="utf-8")

    source = packagist_source(composer_file=composer_file)
    assert source is not None


def test_packagist_cli_main_composer_file(tmp_path, monkeypatch):
    from cognee_community_connector_packagist.cli import main as cli_main

    composer_json = tmp_path / "composer.json"
    composer_json.write_text('{"require": {"monolog/monolog": "^2.0"}}', encoding="utf-8")

    def mock_request(self, endpoint, params=None):
        if "monolog/monolog" in endpoint:
            return MOCK_PKG_MONOLOG
        if "security-advisories" in endpoint:
            return {"advisories": {}}
        return {}

    monkeypatch.setattr(PackagistClient, "request", mock_request)
    ret = cli_main(["--composer-file", str(composer_json), "--packages", ""])
    assert ret == 0


def test_packagist_cli_main_vendor_and_abandoned(monkeypatch):
    from cognee_community_connector_packagist.cli import main as cli_main

    def mock_list_vendor(self, vendor):
        return ["swiftmailer/swiftmailer"]

    def mock_request(self, endpoint, params=None):
        if "swiftmailer" in endpoint:
            return MOCK_PKG_SWIFTMAILER
        return {"advisories": {}}

    monkeypatch.setattr(PackagistClient, "list_vendor_packages", mock_list_vendor)
    monkeypatch.setattr(PackagistClient, "request", mock_request)

    ret = cli_main(["--vendor", "swiftmailer", "--packages", "", "--exclude-abandoned"])
    assert ret == 0
