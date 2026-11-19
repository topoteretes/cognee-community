"""Hermetic unit tests for the Lighthouse CI connector."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import pytest

from cognee_community_connector_lighthouse_ci.lighthouse_ci import (
    LIGHTHOUSE_CI_SOURCE_NAME,
    format_lighthouse_report_markdown,
    lighthouse_ci_source,
    parse_lighthouse_report,
    sanitize_url,
)

SAMPLE_LHR = {
    "lighthouseVersion": "12.0.0",
    "fetchTime": "2026-10-27T06:30:00.000Z",
    "requestedUrl": "https://example.com/checkout?token=supersecret&page=2",
    "finalDisplayedUrl": "https://example.com/checkout?token=supersecret&page=2",
    "categories": {
        "performance": {"title": "Performance", "score": 0.85},
        "accessibility": {"title": "Accessibility", "score": 0.90},
    },
    "audits": {
        "largest-contentful-paint": {
            "title": "Largest Contentful Paint",
            "score": 0.85,
            "displayValue": "2.4 s",
            "numericValue": 2400.0,
        },
        "color-contrast": {
            "title": "Background and foreground colors do not have sufficient contrast ratio",
            "score": 0.0,
            "displayValue": "2 elements",
            "description": "Low contrast elements detected.",
        },
    },
}


def test_sanitize_url():
    raw = "https://example.com/app?token=secret123&api_key=xyz&user=john"
    sanitized = sanitize_url(raw)
    assert "token=%5BREDACTED%5D" in sanitized or "token=[REDACTED]" in sanitized
    assert "api_key=%5BREDACTED%5D" in sanitized or "api_key=[REDACTED]" in sanitized
    assert "user=john" in sanitized


def test_parse_lighthouse_report_success():
    parsed = parse_lighthouse_report(SAMPLE_LHR)
    assert parsed["lighthouse_version"] == "12.0.0"
    assert "token=[REDACTED]" in parsed["clean_url"] or "token=%5BREDACTED%5D" in parsed["clean_url"]
    assert parsed["categories"]["performance"]["score"] == 85
    assert parsed["categories"]["accessibility"]["score"] == 90
    assert len(parsed["findings"]) == 1
    assert parsed["findings"][0]["id"] == "color-contrast"


def test_parse_lighthouse_report_missing_url():
    bad_lhr = {"lighthouseVersion": "12.0.0", "audits": {}}
    with pytest.raises(ValueError, match="missing requestedUrl"):
        parse_lighthouse_report(bad_lhr)


def test_parse_lighthouse_report_invalid_json():
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tmp:
        tmp.write("invalid json content")
        tmp_path = Path(tmp.name)

    try:
        with pytest.raises(ValueError, match="not valid JSON"):
            parse_lighthouse_report(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)


def test_format_lighthouse_report_markdown():
    parsed = parse_lighthouse_report(SAMPLE_LHR)
    md = format_lighthouse_report_markdown(parsed, build_id="b-100", commit_sha="c-200")

    assert "# [Lighthouse CI] Audit Report:" in md
    assert "Build ID:** b-100" in md
    assert "Commit SHA:** c-200" in md
    assert "Performance:** 85 / 100" in md
    assert "Accessibility:** 90 / 100" in md
    assert "Largest Contentful Paint:** 2.4 s" in md
    assert "color-contrast" in md


def test_lighthouse_ci_source_empty_dir():
    pytest.importorskip("dlt")
    with tempfile.TemporaryDirectory() as empty_dir:
        with pytest.raises(ValueError, match="No valid Lighthouse CI report files found"):
            lighthouse_ci_source(reports_dir=empty_dir)


def test_lighthouse_ci_source_with_dlt():
    dlt = pytest.importorskip("dlt")
    with tempfile.TemporaryDirectory() as tmp_dir:
        report_file = Path(tmp_dir) / "lhr-test.json"
        report_file.write_text(json.dumps(SAMPLE_LHR), encoding="utf-8")

        source = lighthouse_ci_source(reports_dir=tmp_dir)
        assert getattr(source, "cognee_document_source", None) == LIGHTHOUSE_CI_SOURCE_NAME
