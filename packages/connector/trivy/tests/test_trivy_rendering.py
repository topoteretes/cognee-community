"""Unit tests for Trivy markdown document renderers and secret redaction."""

from cognee_community_connector_trivy.trivy import (
    _redact_secret_text,
    render_misconfiguration,
    render_secret,
    render_summary,
    render_vulnerability,
)


def test_redact_secret_text():
    assert _redact_secret_text("") == "[REDACTED]"
    assert _redact_secret_text("12345") == "[REDACTED]"
    redacted = _redact_secret_text("AKIAIOSFODNN7EXAMPLE")
    assert "AKIAIOSFODNN7EXAMPLE" not in redacted
    assert "[REDACTED]" in redacted
    assert redacted.startswith("AK")
    assert redacted.endswith("LE")


def test_render_vulnerability():
    vuln = {
        "VulnerabilityID": "CVE-2023-38545",
        "PkgName": "libcurl4",
        "InstalledVersion": "7.74.0-1.3",
        "FixedVersion": "7.74.0-1.3+deb11u10",
        "Severity": "CRITICAL",
        "Title": "curl: SOCKS5 heap buffer overflow",
        "Description": "Flaw allows heap buffer overflow in SOCKS5 proxy handshake.",
        "PrimaryURL": "https://avd.aquasec.com/nvd/cve-2023-38545",
        "CVSS": {"nvd": {"V3Score": 9.8}},
    }
    doc = render_vulnerability("app:v1", "debian-pkgs", vuln)

    assert doc["id"].startswith("trivy:vuln:")
    assert "CVE-2023-38545" in doc["id"]
    assert doc["_deleted"] is False
    assert "**Severity:** CRITICAL" in doc["content"]
    assert "**Vulnerable Package:** `libcurl4`" in doc["content"]
    assert "**Installed Version:** `7.74.0-1.3`" in doc["content"]
    assert "**Fixed Version:** `7.74.0-1.3+deb11u10`" in doc["content"]
    assert "nvd V3: 9.8" in doc["content"]
    assert "https://avd.aquasec.com/nvd/cve-2023-38545" in doc["content"]


def test_render_misconfiguration():
    misconfig = {
        "Type": "Kubernetes Security Check",
        "ID": "KSV001",
        "Title": "Can elevate its own privileges",
        "Description": "A program can run with higher privileges.",
        "Severity": "HIGH",
        "Resolution": "Set 'securityContext.allowPrivilegeEscalation' to 'false'.",
        "Status": "FAIL",
    }
    doc = render_misconfiguration("k8s-cluster", "deployment.yaml", misconfig)

    assert doc["id"].startswith("trivy:misconfig:")
    assert "KSV001" in doc["id"]
    assert "**Check Status:** FAIL" in doc["content"]
    assert "**Severity:** HIGH" in doc["content"]
    assert "securityContext.allowPrivilegeEscalation" in doc["content"]


def test_render_secret_never_leaks_raw_credential():
    raw_secret = "AKIAIOSFODNN7EXAMPLE"
    secret = {
        "RuleID": "aws-access-key-id",
        "Category": "AWS",
        "Severity": "CRITICAL",
        "Title": "AWS Access Key ID",
        "StartLine": 14,
        "EndLine": 14,
        "Match": raw_secret,
    }
    doc = render_secret("repo-scan", "configs/.env", secret)

    assert doc["id"].startswith("trivy:secret:")
    # Ensure raw secret string is completely absent from content
    assert raw_secret not in doc["content"]
    assert "[REDACTED]" in doc["content"]
    assert "**Category:** AWS" in doc["content"]
    assert "Lines 14-14" in doc["content"]


def test_render_summary():
    results = [
        {
            "Vulnerabilities": [
                {"Severity": "CRITICAL"},
                {"Severity": "CRITICAL"},
                {"Severity": "HIGH"},
            ],
            "Misconfigurations": [{"ID": "M1"}, {"ID": "M2"}],
            "Secrets": [{"RuleID": "S1"}],
        }
    ]
    metadata = {"OS": {"Family": "ubuntu", "Name": "22.04"}}
    doc = render_summary("docker.io/myorg/web:1.0", "container_image", metadata, results)

    assert doc["id"] == "trivy:summary:docker.io_myorg_web:1.0"
    assert "**Critical Severity CVEs:** 2" in doc["content"]
    assert "**High Severity CVEs:** 1" in doc["content"]
    assert "**Total Misconfigurations:** 2" in doc["content"]
    assert "**Total Leaked Secrets:** 1" in doc["content"]
    assert "ubuntu 22.04" in doc["content"]
