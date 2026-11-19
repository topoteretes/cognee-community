"""Hermetic unit tests for the AbuseIPDB connector."""

from __future__ import annotations

import pytest

from cognee_community_connector_abuseipdb.abuseipdb import (
    ABUSEIPDB_SOURCE_NAME,
    abuseipdb_source,
    format_ip_reputation_markdown,
    is_public_ip,
    resolve_category_names,
)

SAMPLE_IP = {
    "ipAddress": "198.51.100.42",
    "abuseConfidenceScore": 100,
    "countryCode": "US",
    "countryName": "United States",
    "isp": "Malicious Cloud Compute LLC",
    "domain": "badcloud.example",
    "usageType": "Data Center/Web Hosting/Transit",
    "isTor": False,
    "totalReports": 412,
    "lastReportedAt": "2026-10-25T14:22:00Z",
    "categoryIds": [18, 22, 14],
}


def test_is_public_ip():
    assert is_public_ip("198.51.100.42") is True
    assert is_public_ip("8.8.8.8") is True
    assert is_public_ip("1.1.1.1") is True

    # Private / loopback / link-local
    assert is_public_ip("127.0.0.1") is False
    assert is_public_ip("10.0.0.1") is False
    assert is_public_ip("192.168.1.1") is False
    assert is_public_ip("172.16.0.1") is False
    assert is_public_ip("169.254.1.1") is False
    assert is_public_ip("not-an-ip") is False
    assert is_public_ip("") is False


def test_resolve_category_names():
    resolved = resolve_category_names([18, 22, 4, 14, 999])
    assert "Brute-Force" in resolved
    assert "SSH" in resolved
    assert "DDoS Attack" in resolved
    assert "Port Scan" in resolved
    assert "Category 999" in resolved


def test_format_ip_reputation_markdown():
    md = format_ip_reputation_markdown(SAMPLE_IP)

    assert "# [AbuseIPDB] Threat Intelligence: 198.51.100.42" in md
    assert "198.51.100.42" in md
    assert "100%" in md
    assert "US (United States)" in md
    assert "Malicious Cloud Compute LLC" in md
    assert "Total Abuse Reports:** 412" in md
    assert "Brute-Force" in md
    assert "SSH" in md
    assert "CRITICAL SEVERITY" in md
    assert "https://www.abuseipdb.com/check/198.51.100.42" in md


def test_abuseipdb_source_missing_api_key():
    pytest.importorskip("dlt")
    with pytest.raises(ValueError, match="AbuseIPDB API key required"):
        abuseipdb_source(ips=["198.51.100.42"], api_key=None)


def test_abuseipdb_source_with_dlt():
    dlt = pytest.importorskip("dlt")
    source = abuseipdb_source(snapshot_data=[SAMPLE_IP], min_confidence=50)
    assert getattr(source, "cognee_document_source", None) == ABUSEIPDB_SOURCE_NAME
