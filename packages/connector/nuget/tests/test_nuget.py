"""Hermetic unit test suite for the Cognee NuGet v3 Connector."""

import json

import httpx
import pytest

from cognee_community_connector_nuget.nuget import (
    NugetClient,
    NugetConnectorError,
    NugetRenderOptions,
    extract_packages_from_dotnet_file,
    nuget_source,
    render_nuget_document,
    sync_nuget_records,
)

# Mock NuGet v3 API fixtures
MOCK_SERVICE_INDEX = {
    "version": "3.0.0",
    "resources": [
        {
            "@id": "https://api.nuget.org/v3/registration5-gz-semver2/",
            "@type": "RegistrationsBaseUrl/Versioned",
            "comment": "Base URL of Azure storage where NuGet package registration info is stored",
        },
        {
            "@id": "https://azuresearch-usnc.nuget.org/query",
            "@type": "SearchQueryService",
            "comment": "Query endpoint of NuGet Search service",
        },
    ],
}

MOCK_REGISTRATION_NEWTONSOFT = {
    "count": 1,
    "items": [
        {
            "@id": "https://api.nuget.org/v3/registration5-gz-semver2/newtonsoft.json/page/1.0.0/13.0.3.json",
            "count": 1,
            "items": [
                {
                    "catalogEntry": {
                        "id": "Newtonsoft.Json",
                        "version": "13.0.3",
                        "authors": "James Newton-King",
                        "description": "Json.NET is a popular JSON framework for .NET",
                        "projectUrl": "https://www.newtonsoft.com/json",
                        "licenseExpression": "MIT",
                        "totalDownloads": 4500000000,
                        "listed": True,
                        "published": "2023-03-08T06:00:00+00:00",
                        "dependencyGroups": [
                            {
                                "targetFramework": "net8.0",
                                "dependencies": [],
                            },
                            {
                                "targetFramework": "netstandard2.0",
                                "dependencies": [{"id": "Microsoft.CSharp", "range": "[4.3.0, )"}],
                            },
                        ],
                        "vulnerabilities": [
                            {
                                "advisoryUrl": "https://github.com/advisories/GHSA-5crp-9r3c-p9vr",
                                "severity": "High",
                            }
                        ],
                    }
                }
            ],
        }
    ],
}

MOCK_REGISTRATION_DEPRECATED = {
    "count": 1,
    "items": [
        {
            "items": [
                {
                    "catalogEntry": {
                        "id": "WindowsAzure.Storage",
                        "version": "9.3.3",
                        "authors": "Microsoft",
                        "description": "Client library for Microsoft Azure Storage services",
                        "projectUrl": "https://github.com/Azure/azure-storage-net",
                        "licenseExpression": "Apache-2.0",
                        "totalDownloads": 200000000,
                        "listed": True,
                        "published": "2018-08-01T00:00:00+00:00",
                        "deprecation": {
                            "reasons": ["Legacy"],
                            "message": "Library split into individual packages.",
                            "alternatePackage": {
                                "id": "Azure.Storage.Common",
                                "range": "*",
                            },
                        },
                    }
                }
            ]
        }
    ],
}

MOCK_REGISTRATION_UNLISTED = {
    "count": 1,
    "items": [
        {
            "items": [
                {
                    "catalogEntry": {
                        "id": "Internal.Package",
                        "version": "0.1.0-alpha",
                        "authors": "Dev",
                        "listed": False,
                        "published": "1900-01-01T00:00:00+00:00",
                    }
                }
            ]
        }
    ],
}

MOCK_SEARCH_RESULTS = {
    "totalHits": 2,
    "data": [
        {"id": "Serilog", "title": "Serilog"},
        {"id": "Serilog.Sinks.Console", "title": "Serilog.Sinks.Console"},
    ],
}


class FakeNugetTransport(httpx.BaseTransport):
    """Hermetic mock HTTP transport simulating NuGet v3 REST endpoints."""

    def __init__(self, simulate_rate_limit: bool = False):
        self.simulate_rate_limit = simulate_rate_limit
        self.rate_limit_counter = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)

        if self.simulate_rate_limit and self.rate_limit_counter == 0:
            self.rate_limit_counter += 1
            return httpx.Response(status_code=429, headers={"Retry-After": "0"})

        if "v3/index.json" in url_str:
            return httpx.Response(status_code=200, json=MOCK_SERVICE_INDEX)

        if "newtonsoft.json/index.json" in url_str:
            return httpx.Response(status_code=200, json=MOCK_REGISTRATION_NEWTONSOFT)

        if "windowsazure.storage/index.json" in url_str:
            return httpx.Response(status_code=200, json=MOCK_REGISTRATION_DEPRECATED)

        if "internal.package/index.json" in url_str:
            return httpx.Response(status_code=200, json=MOCK_REGISTRATION_UNLISTED)

        if "query" in url_str:
            return httpx.Response(status_code=200, json=MOCK_SEARCH_RESULTS)

        if "nonexistent/index.json" in url_str:
            return httpx.Response(status_code=404, json={"error": "Not Found"})

        return httpx.Response(status_code=404, json={"error": "Not Found"})


def create_mock_client(simulate_rate_limit: bool = False) -> NugetClient:
    transport = FakeNugetTransport(simulate_rate_limit=simulate_rate_limit)
    http_client = httpx.Client(transport=transport)
    return NugetClient(client=http_client)


# ─── Tests ────────────────────────────────────────────────────────────────────


def test_service_index_discovery():
    client = create_mock_client()
    client.discover_endpoints()
    assert client.registration_base_url == "https://api.nuget.org/v3/registration5-gz-semver2"
    assert client.search_query_url == "https://azuresearch-usnc.nuget.org/query"


def test_get_package_registration_success():
    client = create_mock_client()
    entries = client.get_package_registration("Newtonsoft.Json")
    assert len(entries) == 1
    assert entries[0]["id"] == "Newtonsoft.Json"
    assert entries[0]["version"] == "13.0.3"
    assert entries[0]["licenseExpression"] == "MIT"


def test_get_package_registration_not_found():
    client = create_mock_client()
    with pytest.raises(NugetConnectorError) as exc_info:
        client.get_package_registration("nonexistent")
    assert exc_info.value.status_code == 404


def test_rate_limit_backoff():
    client = create_mock_client(simulate_rate_limit=True)
    entries = client.get_package_registration("Newtonsoft.Json")
    assert len(entries) == 1


def test_search_packages():
    client = create_mock_client()
    results = client.search_packages("serilog")
    assert len(results) == 2
    assert "Serilog" in results


def test_render_nuget_document_active_with_vulnerabilities():
    entries = MOCK_REGISTRATION_NEWTONSOFT["items"][0]["items"]
    catalog_entries = [e["catalogEntry"] for e in entries]

    doc = render_nuget_document("Newtonsoft.Json", catalog_entries)

    assert doc["id"] == "nuget:newtonsoft.json"
    assert doc["title"] == "NuGet: Newtonsoft.Json"
    assert doc["_deleted"] is False
    assert doc["is_deprecated"] is False
    assert doc["alternate_package"] is None
    assert doc["vulnerability_count"] == 1

    # Content checks
    assert "# NuGet Package: Newtonsoft.Json" in doc["content"]
    assert "License: MIT" in doc["content"]
    assert "Framework [net8.0]" in doc["content"]
    assert "GHSA-5crp-9r3c-p9vr" in doc["content"]


def test_render_nuget_document_deprecated_with_alternate():
    entries = MOCK_REGISTRATION_DEPRECATED["items"][0]["items"]
    catalog_entries = [e["catalogEntry"] for e in entries]

    doc = render_nuget_document("WindowsAzure.Storage", catalog_entries)

    assert doc["id"] == "nuget:windowsazure.storage"
    assert doc["is_deprecated"] is True
    assert doc["alternate_package"] == "Azure.Storage.Common"
    assert "⚠️ DEPRECATED" in doc["content"]
    assert "Author recommends: 'Azure.Storage.Common'" in doc["content"]


def test_sync_nuget_records_normal():
    client = create_mock_client()
    docs = list(sync_nuget_records(client, ["Newtonsoft.Json"]))
    assert len(docs) == 1
    assert docs[0]["id"] == "nuget:newtonsoft.json"
    assert docs[0]["_deleted"] is False


def test_sync_nuget_records_tombstone_on_404():
    client = create_mock_client()
    docs = list(sync_nuget_records(client, ["nonexistent"]))
    assert len(docs) == 1
    assert docs[0]["id"] == "nuget:nonexistent"
    assert docs[0]["_deleted"] is True
    assert "Deleted" in docs[0]["title"]


def test_sync_nuget_records_tombstone_unlisted():
    client = create_mock_client()
    docs = list(sync_nuget_records(client, ["Internal.Package"]))
    assert len(docs) == 1
    assert docs[0]["id"] == "nuget:internal.package"
    assert docs[0]["_deleted"] is True
    assert "Unlisted" in docs[0]["title"]


def test_sync_nuget_records_exclude_deprecated():
    client = create_mock_client()
    options = NugetRenderOptions(exclude_deprecated=True)
    docs = list(sync_nuget_records(client, ["WindowsAzure.Storage"], options=options))
    assert len(docs) == 1
    assert docs[0]["id"] == "nuget:windowsazure.storage"
    assert docs[0]["_deleted"] is True
    assert "Deprecated" in docs[0]["title"]


def test_extract_packages_from_csproj(tmp_path):
    csproj_file = tmp_path / "Service.csproj"
    csproj_file.write_text(
        """<Project Sdk="Microsoft.NET.Sdk">
  <ItemGroup>
    <PackageReference Include="Newtonsoft.Json" Version="13.0.3" />
    <PackageReference Include="Serilog" Version="3.1.1" />
  </ItemGroup>
</Project>""",
        encoding="utf-8",
    )

    pkgs = extract_packages_from_dotnet_file(csproj_file)
    assert pkgs == ["Newtonsoft.Json", "Serilog"]


def test_extract_packages_from_props(tmp_path):
    props_file = tmp_path / "Directory.Packages.props"
    props_file.write_text(
        """<Project>
  <ItemGroup>
    <PackageVersion Include="Microsoft.Extensions.Logging" Version="8.0.0" />
    <PackageVersion Include="xunit" Version="2.6.2" />
  </ItemGroup>
</Project>""",
        encoding="utf-8",
    )

    pkgs = extract_packages_from_dotnet_file(props_file)
    assert pkgs == ["Microsoft.Extensions.Logging", "xunit"]


def test_extract_packages_from_lock_json(tmp_path):
    lock_file = tmp_path / "packages.lock.json"
    lock_file.write_text(
        json.dumps(
            {
                "version": 1,
                "dependencies": {
                    "net8.0": {
                        "Newtonsoft.Json": {"type": "Direct", "resolved": "13.0.3"},
                        "Polly": {"type": "Direct", "resolved": "8.3.0"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    pkgs = extract_packages_from_dotnet_file(lock_file)
    assert pkgs == ["Newtonsoft.Json", "Polly"]


def test_extract_packages_file_not_found():
    with pytest.raises(FileNotFoundError):
        extract_packages_from_dotnet_file("missing.csproj")


def test_nuget_source_execution(tmp_path):
    csproj_file = tmp_path / "App.csproj"
    xml_snippet = (
        '<Project><ItemGroup><PackageReference Include="Newtonsoft.Json" /></ItemGroup></Project>'
    )
    csproj_file.write_text(xml_snippet, encoding="utf-8")
    source = nuget_source(
        project_file=csproj_file,
        packages=["Serilog"],
    )
    assert source is not None


def test_cli_main_with_project_file(tmp_path, monkeypatch):
    from cognee_community_connector_nuget.cli import main as cli_main

    csproj = tmp_path / "Test.csproj"
    csproj.write_text(
        '<Project><ItemGroup><PackageReference Include="Newtonsoft.Json" /></ItemGroup></Project>',
        encoding="utf-8",
    )

    def mock_request(self, url, params=None):
        if "query" in url:
            return {"data": [{"id": "Newtonsoft.Json"}]}
        return MOCK_REGISTRATION_NEWTONSOFT

    monkeypatch.setattr(NugetClient, "request", mock_request)
    ret = cli_main(["--project-file", str(csproj), "--search", "json"])
    assert ret == 0


def test_cli_main_exclude_deprecated(monkeypatch):
    from cognee_community_connector_nuget.cli import main as cli_main

    def mock_request(self, url, params=None):
        return MOCK_REGISTRATION_DEPRECATED

    monkeypatch.setattr(NugetClient, "request", mock_request)
    ret = cli_main(["--packages", "WindowsAzure.Storage", "--exclude-deprecated"])
    assert ret == 0
