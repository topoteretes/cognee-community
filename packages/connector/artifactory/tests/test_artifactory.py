"""100% offline unit tests for the JFrog Artifactory connector."""

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from cognee_community_connector_artifactory.artifactory import (
    ARTIFACTORY_ARTIFACTS_TABLE_NAME,
    ARTIFACTORY_BUILDS_TABLE_NAME,
    ARTIFACTORY_REPOSITORIES_TABLE_NAME,
    ARTIFACTORY_SOURCE_NAME,
    DOCUMENT_SOURCE_ATTR,
    _artifact_to_document,
    _build_to_document,
    _repo_to_document,
    artifactory_source,
)
from cognee_community_connector_artifactory.client import (
    ArtifactoryAuthError,
    ArtifactoryClient,
    ArtifactoryNotFoundError,
)

# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def sample_repository() -> dict[str, Any]:
    return {
        "key": "docker-prod-local",
        "rclass": "local",
        "packageType": "docker",
        "description": "Production Docker images and container registries",
        "repoLayoutRef": "simple-default",
        "notes": "Nightly cleanup runs on untagged images",
    }


@pytest.fixture
def sample_artifact_item() -> dict[str, Any]:
    return {
        "name": "manifest.json",
        "path": "services/auth-service/v1.4.0",
        "size": 2048,
        "created": "2026-09-15T08:30:00Z",
        "modified": "2026-09-15T08:30:00Z",
        "actual_sha1": "da39a3ee5e6b4b0d3255bfef95601890afd80709",
        "actual_md5": "d41d8cd98f00b204e9800998ecf8427e",
        "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "downloadUri": "https://company.jfrog.io/artifactory/docker-prod-local/services/auth-service/v1.4.0/manifest.json",
    }


@pytest.fixture
def sample_properties() -> dict[str, Any]:
    return {
        "build.name": ["auth-service-pipeline"],
        "build.number": ["142"],
        "git.commit": ["9f8a2b3c4d5e"],
        "release.status": ["production-ready"],
    }


@pytest.fixture
def sample_build_info() -> dict[str, Any]:
    return {
        "started": "2026-09-15T08:25:00.000+0000",
        "durationMillis": 185000,
        "agent": {"name": "GitHub Actions", "version": "2.310.0"},
        "vcs": [
            {
                "url": "https://github.com/company/auth-service.git",
                "revision": "9f8a2b3c4d5e6f7a8b9c",
                "branch": "main",
            }
        ],
        "modules": [
            {
                "id": "com.company:auth-service:1.4.0",
                "artifacts": [
                    {
                        "name": "auth-service-1.4.0.jar",
                        "sha256": (
                            "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
                        ),
                    }
                ],
                "dependencies": [
                    {
                        "id": "org.springframework.boot:spring-boot-starter:3.2.0",
                        "sha1": "11223344",
                    },
                    {"id": "io.jsonwebtoken:jjwt-api:0.12.3", "sha1": "55667788"},
                ],
            }
        ],
    }


# ===========================================================================
# Client validation & security tests
# ===========================================================================


def test_client_missing_url(monkeypatch):
    monkeypatch.delenv("ARTIFACTORY_URL", raising=False)
    monkeypatch.delenv("JFROG_URL", raising=False)
    with pytest.raises(ValueError, match="JFrog Artifactory URL required"):
        ArtifactoryClient(base_url=None, token="token-123")


def test_client_invalid_url_scheme():
    with pytest.raises(ValueError, match="Invalid JFrog Artifactory URL"):
        ArtifactoryClient(base_url="ftp://artifactory.local", token="token-123")

    with pytest.raises(ValueError, match="Invalid JFrog Artifactory URL"):
        ArtifactoryClient(base_url="not-a-url", token="token-123")


def test_client_missing_credentials(monkeypatch):
    monkeypatch.delenv("ARTIFACTORY_TOKEN", raising=False)
    monkeypatch.delenv("JFROG_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("ARTIFACTORY_API_KEY", raising=False)
    monkeypatch.delenv("ARTIFACTORY_USERNAME", raising=False)
    monkeypatch.delenv("ARTIFACTORY_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="JFrog Artifactory authentication required"):
        ArtifactoryClient(base_url="https://company.jfrog.io")


def test_client_url_normalization():
    # If base_url does not include /artifactory, it is appended
    c1 = ArtifactoryClient(base_url="https://company.jfrog.io", token="tok")
    assert c1.base_url == "https://company.jfrog.io/artifactory"

    # If base_url already includes /artifactory, it is preserved
    c2 = ArtifactoryClient(base_url="https://company.jfrog.io/artifactory/", token="tok")
    assert c2.base_url == "https://company.jfrog.io/artifactory"


def test_client_auth_schemes():
    # Bearer token
    c_token = ArtifactoryClient(base_url="https://company.jfrog.io", token="bearer-token-123")
    assert c_token._headers()["Authorization"] == "Bearer bearer-token-123"
    assert "X-JFrog-Art-Api" not in c_token._headers()

    # API Key
    c_key = ArtifactoryClient(base_url="https://company.jfrog.io", api_key="api-key-456")
    assert c_key._headers()["X-JFrog-Art-Api"] == "api-key-456"
    assert "Authorization" not in c_key._headers()

    # Basic Auth
    c_basic = ArtifactoryClient(
        base_url="https://company.jfrog.io", username="admin", password="password123"
    )
    assert c_basic._auth() == ("admin", "password123")


def test_client_credential_redaction():
    client = ArtifactoryClient(
        base_url="https://company.jfrog.io",
        token="secret-access-token-999",
    )
    raw_error = "Connection failed to https://company.jfrog.io with token secret-access-token-999"
    redacted = client.redact(raw_error)
    assert "secret-access-token-999" not in redacted
    assert "[REDACTED]" in redacted


# ===========================================================================
# Client HTTP operations & error handling
# ===========================================================================


def test_client_auth_error_handling():
    client = ArtifactoryClient(base_url="https://company.jfrog.io", token="bad-token")
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 401
    with (
        patch("httpx.Client.request", return_value=mock_resp),
        pytest.raises(ArtifactoryAuthError, match="auth failed"),
    ):
        client.request("GET", "/api/repositories")


def test_client_not_found_error_handling():
    client = ArtifactoryClient(base_url="https://company.jfrog.io", token="good-token")
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 404
    with (
        patch("httpx.Client.request", return_value=mock_resp),
        pytest.raises(ArtifactoryNotFoundError, match="resource not found"),
    ):
        client.request("GET", "/api/repositories/nonexistent")


def test_client_retry_and_recovery():
    client = ArtifactoryClient(base_url="https://company.jfrog.io", token="tok")

    resp_fail = MagicMock(spec=httpx.Response)
    resp_fail.status_code = 503

    resp_ok = MagicMock(spec=httpx.Response)
    resp_ok.status_code = 200
    resp_ok.content = b'{"status": "recovered"}'
    resp_ok.json.return_value = {"status": "recovered"}

    with patch("httpx.Client.request", side_effect=[resp_fail, resp_ok]), patch("time.sleep"):
        res = client.request("GET", "/api/system/ping")
        assert res == {"status": "recovered"}


def test_client_get_repositories():
    client = ArtifactoryClient(base_url="https://company.jfrog.io", token="tok")
    mock_repos = [
        {"key": "docker-local", "type": "local", "packageType": "docker"},
        {"key": "maven-remote", "type": "remote", "packageType": "maven"},
    ]
    with patch.object(client, "request", return_value=mock_repos) as mock_req:
        res = client.get_repositories(repo_type="local", package_type="docker")
        mock_req.assert_called_once_with(
            "GET", "/api/repositories", params={"type": "local", "packageType": "docker"}
        )
        assert len(res) == 2
        assert res[0]["key"] == "docker-local"


def test_client_get_build_runs():
    client = ArtifactoryClient(base_url="https://company.jfrog.io", token="tok")
    mock_builds = {"builds": [{"uri": "/pipeline-a"}, {"uri": "/pipeline-b"}]}
    with patch.object(client, "request", return_value=mock_builds):
        runs = client.get_build_runs()
        assert len(runs) == 2
        assert runs[0]["build_name"] == "pipeline-a"


def test_client_execute_aql():
    client = ArtifactoryClient(base_url="https://company.jfrog.io", token="tok")
    mock_aql_res = {"results": [{"name": "pkg.tgz", "size": 1024}]}
    with patch.object(client, "request", return_value=mock_aql_res) as mock_req:
        res = client.execute_aql('items.find({"repo":"npm-local"})')
        mock_req.assert_called_once()
        assert len(res) == 1
        assert res[0]["name"] == "pkg.tgz"


# ===========================================================================
# Markdown document rendering tests
# ===========================================================================


def test_repo_to_document_rendering(sample_repository):
    doc = _repo_to_document(sample_repository, "https://company.jfrog.io/artifactory")
    assert doc["id"] == "artifactory_repo_docker-prod-local"
    assert "docker-prod-local" in doc["title"]
    assert "docker" in doc["title"]
    content = doc["content"]
    assert "# JFrog Artifactory Repository: docker-prod-local" in content
    assert "**Repository Key**: `docker-prod-local`" in content
    assert "**Package Ecosystem**: `docker`" in content
    assert "Production Docker images and container registries" in content
    assert "Nightly cleanup runs on untagged images" in content


def test_artifact_to_document_rendering(sample_artifact_item, sample_properties):
    doc = _artifact_to_document(
        "docker-prod-local",
        sample_artifact_item,
        sample_properties,
        "https://company.jfrog.io/artifactory",
    )
    assert (
        doc["id"] == "artifactory_art_docker-prod-local_services_auth-service_v1.4.0_manifest.json"
    )
    assert doc["title"] == "Artifactory Artifact: manifest.json in docker-prod-local"
    content = doc["content"]
    assert "# Artifactory Artifact: manifest.json" in content
    assert "**Repository**: `docker-prod-local`" in content
    assert "**File Name**: `manifest.json`" in content
    assert (
        "**SHA-256**: `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`" in content
    )
    assert "**SHA-1**: `da39a3ee5e6b4b0d3255bfef95601890afd80709`" in content
    assert "**MD5**: `d41d8cd98f00b204e9800998ecf8427e`" in content
    assert "**build.name**: `auth-service-pipeline`" in content
    assert "**git.commit**: `9f8a2b3c4d5e`" in content


def test_build_to_document_rendering(sample_build_info):
    doc = _build_to_document(
        "auth-service-pipeline",
        "142",
        sample_build_info,
        "https://company.jfrog.io/artifactory",
    )
    assert doc["id"] == "artifactory_build_auth-service-pipeline_142"
    assert doc["title"] == "Artifactory Build: auth-service-pipeline #142"
    content = doc["content"]
    assert "# Artifactory CI/CD Build Run: auth-service-pipeline #142" in content
    assert "**CI Agent**: GitHub Actions 2.310.0" in content
    assert "**Git Repository**: https://github.com/company/auth-service.git" in content
    assert "**Commit Revision**: `9f8a2b3c4d5e6f7a8b9c`" in content
    assert "### Module: com.company:auth-service:1.4.0" in content
    assert "`auth-service-1.4.0.jar`" in content
    assert "`org.springframework.boot:spring-boot-starter:3.2.0`" in content


# ===========================================================================
# dlt source and pipeline invariants
# ===========================================================================


def test_artifactory_source_attributes():
    mock_client = MagicMock(spec=ArtifactoryClient)
    mock_client.base_url = "https://company.jfrog.io/artifactory"
    source = artifactory_source(client=mock_client)

    # Invariant 1: Document source attribute tag for Cognee
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == ARTIFACTORY_SOURCE_NAME

    # Invariant 2: Resources registered
    resources = {r.name: r for r in source.selected_resources.values()}
    assert ARTIFACTORY_REPOSITORIES_TABLE_NAME in resources
    assert ARTIFACTORY_ARTIFACTS_TABLE_NAME in resources
    assert ARTIFACTORY_BUILDS_TABLE_NAME in resources

    # Invariant 3: Write disposition is replace for automatic orphan cleanup
    assert resources[ARTIFACTORY_REPOSITORIES_TABLE_NAME].write_disposition == "replace"
    assert resources[ARTIFACTORY_ARTIFACTS_TABLE_NAME].write_disposition == "replace"
    assert resources[ARTIFACTORY_BUILDS_TABLE_NAME].write_disposition == "replace"


def test_artifactory_source_selective_resources():
    mock_client = MagicMock(spec=ArtifactoryClient)
    mock_client.base_url = "https://company.jfrog.io/artifactory"
    source = artifactory_source(
        client=mock_client,
        include_artifacts=False,
        include_builds=False,
    )

    resources = {r.name: r for r in source.selected_resources.values()}
    assert ARTIFACTORY_REPOSITORIES_TABLE_NAME in resources
    assert ARTIFACTORY_ARTIFACTS_TABLE_NAME not in resources
    assert ARTIFACTORY_BUILDS_TABLE_NAME not in resources


def test_artifactory_source_execution(
    sample_repository, sample_artifact_item, sample_properties, sample_build_info
):
    mock_client = MagicMock(spec=ArtifactoryClient)
    mock_client.base_url = "https://company.jfrog.io/artifactory"
    mock_client.get_repositories.return_value = [sample_repository]
    mock_client.get_repository_details.return_value = sample_repository
    mock_client.execute_aql.return_value = [sample_artifact_item]
    mock_client.get_item_properties.return_value = sample_properties
    mock_client.get_build_runs.side_effect = [
        [{"build_name": "auth-service-pipeline"}],
        [{"build_name": "auth-service-pipeline", "uri": "142"}],
    ]
    mock_client.get_build_info.return_value = sample_build_info

    source = artifactory_source(client=mock_client)

    repo_resource = source.selected_resources[ARTIFACTORY_REPOSITORIES_TABLE_NAME]
    art_resource = source.selected_resources[ARTIFACTORY_ARTIFACTS_TABLE_NAME]
    build_resource = source.selected_resources[ARTIFACTORY_BUILDS_TABLE_NAME]

    repo_rows = list(repo_resource)
    art_rows = list(art_resource)
    build_rows = list(build_resource)

    assert len(repo_rows) == 1
    assert repo_rows[0]["id"] == "artifactory_repo_docker-prod-local"

    assert len(art_rows) == 1
    assert "docker-prod-local" in art_rows[0]["id"]
    assert "manifest.json" in art_rows[0]["title"]

    assert len(build_rows) == 1
    assert build_rows[0]["id"] == "artifactory_build_auth-service-pipeline_142"
    assert "auth-service-pipeline #142" in build_rows[0]["title"]
