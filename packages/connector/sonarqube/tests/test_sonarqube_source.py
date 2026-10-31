"""Factory validation: never echoes the token, validates every parameter."""

import pytest
from fake_sonarqube import FakeSonarQubeSession

from cognee_community_connector_sonarqube import sonarqube_source


def _source(**overrides):
    defaults = {
        "token": "tok-123",
        "base_url": "https://sonarqube.example.com",
        "project_keys": ["proj"],
        "session": FakeSonarQubeSession(projects={"proj": {"name": "Proj"}}),
    }
    defaults.update(overrides)
    return sonarqube_source(**defaults)


def test_requires_token():
    with pytest.raises(ValueError, match="token"):
        sonarqube_source(base_url="https://sonarqube.example.com", project_keys=["proj"])


def test_token_never_in_error_message():
    secret = "super-secret-token-value\n"  # a trailing newline makes _check_token reject it
    with pytest.raises(ValueError) as exc_info:
        sonarqube_source(
            token=secret, base_url="https://sonarqube.example.com", project_keys=["proj"]
        )
    assert "super-secret-token-value" not in str(exc_info.value)


def test_requires_base_url():
    with pytest.raises(ValueError, match="base_url"):
        _source(base_url="")


def test_base_url_must_be_https():
    with pytest.raises(ValueError, match="https"):
        _source(base_url="http://sonarqube.example.com")


def test_base_url_http_allowed_for_localhost():
    source = _source(base_url="http://localhost:9000")
    assert source is not None


def test_base_url_rejects_credentials():
    with pytest.raises(ValueError, match="credentials"):
        _source(base_url="https://user:pass@sonarqube.example.com")


def test_base_url_rejects_path():
    with pytest.raises(ValueError, match="path"):
        _source(base_url="https://sonarqube.example.com/sonar")


def test_base_url_rejects_query():
    with pytest.raises(ValueError, match="query"):
        _source(base_url="https://sonarqube.example.com?x=1")


def test_base_url_strips_trailing_slash():
    source = _source(base_url="https://sonarqube.example.com/")
    assert source is not None


def test_requires_project_keys_or_all_projects():
    with pytest.raises(ValueError, match="project_keys"):
        _source(project_keys=None, all_projects=False)


def test_empty_project_keys_is_an_error():
    with pytest.raises(ValueError, match="non-empty"):
        _source(project_keys=[])


def test_all_projects_true_is_accepted_without_project_keys():
    session = FakeSonarQubeSession(projects={"a": {"name": "A"}})
    source = sonarqube_source(
        token="tok", base_url="https://sonarqube.example.com", all_projects=True, session=session
    )
    assert source is not None


def test_cloud_requires_organization():
    with pytest.raises(ValueError, match="organization"):
        sonarqube_source(
            token="tok",
            base_url="https://sonarcloud.io",
            project_keys=["proj"],
            session=FakeSonarQubeSession(projects={"proj": {"name": "Proj"}}),
        )


def test_cloud_with_organization_is_accepted():
    source = sonarqube_source(
        token="tok",
        base_url="https://sonarcloud.io",
        organization="my-org",
        project_keys=["proj"],
        session=FakeSonarQubeSession(projects={"proj": {"name": "Proj"}}),
    )
    assert source is not None


def test_invalid_severity_is_rejected():
    with pytest.raises(ValueError, match="severities"):
        _source(severities=("NOT_A_SEVERITY",))


def test_empty_severities_is_an_error():
    with pytest.raises(ValueError, match="severities"):
        _source(severities=())


def test_invalid_issue_type_is_rejected():
    with pytest.raises(ValueError, match="issue_types"):
        _source(issue_types=("NOT_A_TYPE",))


def test_requires_at_least_one_include_flag():
    with pytest.raises(ValueError, match="include_"):
        _source(include_issues=False, include_hotspots=False, include_quality_gate=False)


def test_max_issues_per_project_bounds():
    with pytest.raises(ValueError, match="max_issues_per_project"):
        _source(max_issues_per_project=0)
    with pytest.raises(ValueError, match="max_issues_per_project"):
        _source(max_issues_per_project=10_001)


def test_max_hotspots_per_project_bounds():
    with pytest.raises(ValueError, match="max_hotspots_per_project"):
        _source(max_hotspots_per_project=0)


def test_created_after_must_parse():
    with pytest.raises(ValueError, match="created_after"):
        _source(created_after="not-a-date")


def test_created_after_accepts_date_and_datetime():
    assert _source(created_after="2026-01-01") is not None
    assert _source(created_after="2026-01-01T00:00:00+0000") is not None


def test_timeout_must_be_positive():
    with pytest.raises(ValueError, match="timeout"):
        _source(timeout=0)


def test_allow_mass_delete_must_be_bool():
    with pytest.raises(ValueError, match="allow_mass_delete"):
        _source(allow_mass_delete="yes")


def test_resource_name_must_be_snake_case():
    with pytest.raises(ValueError, match="resource_name"):
        _source(resource_name="Not-Snake-Case")


def test_env_fallbacks(monkeypatch):
    monkeypatch.setenv("SONARQUBE_TOKEN", "tok-from-env")
    monkeypatch.setenv("SONARQUBE_URL", "https://sonarqube.example.com")
    monkeypatch.setenv("SONARQUBE_PROJECTS", "proj1, proj2")
    session = FakeSonarQubeSession(projects={"proj1": {"name": "P1"}, "proj2": {"name": "P2"}})
    source = sonarqube_source(session=session)
    assert source is not None


def test_resource_carries_document_source_and_stats():
    source = _source()
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

    assert getattr(source, DOCUMENT_SOURCE_ATTR) == "sonarqube"
    assert hasattr(source, "cognee_sync_stats")
