"""Hermetic unit tests for the CISA KEV connector."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import pytest

from cognee_community_connector_cisa_kev.cisa_kev import (
    CISA_KEV_SOURCE_NAME,
    cisa_kev_source,
    fetch_and_validate_kev_catalog,
    format_vulnerability_markdown,
)

SAMPLE_VALID_CATALOG = {
    "title": "CISA Catalog of Known Exploited Vulnerabilities",
    "catalogVersion": "2026.10.30",
    "dateReleased": "2026-10-30T17:02:45.3665Z",
    "count": 2,
    "vulnerabilities": [
        {
            "cveID": "CVE-2026-118527",
            "vendorProject": "Ivanti",
            "product": "Connect Secure",
            "vulnerabilityName": "Ivanti Connect Secure Generation of Predictable Numbers Vulnerability",
            "dateAdded": "2026-10-30",
            "shortDescription": "Generation of predictable numbers vulnerability allows file upload.",
            "requiredAction": "Apply vendor mitigations according to CISA BOD 26-04.",
            "dueDate": "2026-11-13",
            "knownRansomwareCampaignUse": "Known",
            "notes": "https://nvd.nist.gov/vuln/detail/CVE-2026-118527",
            "cwes": ["CWE-340"],
        },
        {
            "cveID": "CVE-2026-111357",
            "vendorProject": "Ivanti",
            "product": "Connect Secure",
            "vulnerabilityName": "Ivanti Connect Secure Missing Authentication Vulnerability",
            "dateAdded": "2026-10-25",
            "shortDescription": "Missing authentication allows privilege escalation.",
            "requiredAction": "Apply vendor update immediately.",
            "dueDate": "2026-11-02",
            "knownRansomwareCampaignUse": "Unknown",
            "notes": "https://nvd.nist.gov/vuln/detail/CVE-2026-111357",
            "cwes": ["CWE-306"],
        },
    ],
}


def _write_temp_catalog(data: dict) -> Path:
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(data, tmp)
    tmp.close()
    return Path(tmp.name)


def test_format_vulnerability_markdown():
    vuln = SAMPLE_VALID_CATALOG["vulnerabilities"][0]
    md = format_vulnerability_markdown(vuln, catalog_version="2026.10.30")

    assert "[CVE-2026-118527]" in md
    assert "Ivanti" in md
    assert "Connect Secure" in md
    assert "2026-10-30" in md
    assert "2026-11-13" in md
    assert "Known" in md
    assert "CWE-340" in md
    assert "Apply vendor mitigations" in md
    assert "https://nvd.nist.gov/vuln/detail/CVE-2026-118527" in md


def test_validate_kev_catalog_success():
    path = _write_temp_catalog(SAMPLE_VALID_CATALOG)
    try:
        catalog = fetch_and_validate_kev_catalog(path)
        assert catalog["catalogVersion"] == "2026.10.30"
        assert len(catalog["vulnerabilities"]) == 2
    finally:
        path.unlink(missing_ok=True)


def test_validate_kev_catalog_count_mismatch():
    invalid_data = dict(SAMPLE_VALID_CATALOG)
    invalid_data["count"] = 999  # Declared 999, actual is 2

    path = _write_temp_catalog(invalid_data)
    try:
        with pytest.raises(ValueError, match="CISA KEV catalog count mismatch"):
            fetch_and_validate_kev_catalog(path)
    finally:
        path.unlink(missing_ok=True)


def test_validate_kev_catalog_invalid_cve_id():
    invalid_data = json.loads(json.dumps(SAMPLE_VALID_CATALOG))
    invalid_data["vulnerabilities"][0]["cveID"] = "NOT-A-REAL-CVE"

    path = _write_temp_catalog(invalid_data)
    try:
        with pytest.raises(ValueError, match="Invalid CVE identifier format"):
            fetch_and_validate_kev_catalog(path)
    finally:
        path.unlink(missing_ok=True)


def test_validate_kev_catalog_duplicate_cve_id():
    invalid_data = json.loads(json.dumps(SAMPLE_VALID_CATALOG))
    invalid_data["vulnerabilities"][1]["cveID"] = "CVE-2026-118527"  # Duplicate of first

    path = _write_temp_catalog(invalid_data)
    try:
        with pytest.raises(ValueError, match="Duplicate CVE ID detected"):
            fetch_and_validate_kev_catalog(path)
    finally:
        path.unlink(missing_ok=True)


def test_validate_kev_catalog_missing_header():
    invalid_data = {"title": "Incomplete Feed"}
    path = _write_temp_catalog(invalid_data)
    try:
        with pytest.raises(ValueError, match="missing required header field"):
            fetch_and_validate_kev_catalog(path)
    finally:
        path.unlink(missing_ok=True)


def test_cisa_kev_source_with_dlt():
    dlt = pytest.importorskip("dlt")

    path = _write_temp_catalog(SAMPLE_VALID_CATALOG)
    try:
        source = cisa_kev_source(feed_url_or_path=path)
        assert getattr(source, "cognee_document_source", None) == CISA_KEV_SOURCE_NAME

        # Test filtering: known ransomware only
        ransomware_source = cisa_kev_source(
            feed_url_or_path=path, known_ransomware_only=True
        )
        assert ransomware_source is not None
    finally:
        path.unlink(missing_ok=True)
