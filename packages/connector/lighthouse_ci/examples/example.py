"""Runnable example for the Lighthouse CI connector.

Demonstrates parsing, sanitizing, and formatting Lighthouse CI audit reports
for Cognee agent ingestion.
"""

import json
from pathlib import Path
import tempfile

from cognee_community_connector_lighthouse_ci import (
    format_lighthouse_report_markdown,
    lighthouse_ci_source,
    parse_lighthouse_report,
)

SAMPLE_LHR = {
    "lighthouseVersion": "12.0.0",
    "fetchTime": "2026-10-27T06:30:00.000Z",
    "requestedUrl": "https://example.com/checkout?token=secret123&env=prod",
    "finalDisplayedUrl": "https://example.com/checkout?token=secret123&env=prod",
    "categories": {
        "performance": {"title": "Performance", "score": 0.88},
        "accessibility": {"title": "Accessibility", "score": 0.95},
        "best-practices": {"title": "Best Practices", "score": 1.0},
        "seo": {"title": "SEO", "score": 0.92},
    },
    "audits": {
        "largest-contentful-paint": {
            "title": "Largest Contentful Paint",
            "score": 0.88,
            "displayValue": "2.1 s",
            "numericValue": 2100.0,
        },
        "first-contentful-paint": {
            "title": "First Contentful Paint",
            "score": 0.95,
            "displayValue": "1.2 s",
            "numericValue": 1200.0,
        },
        "total-blocking-time": {
            "title": "Total Blocking Time",
            "score": 0.90,
            "displayValue": "120 ms",
            "numericValue": 120.0,
        },
        "cumulative-layout-shift": {
            "title": "Cumulative Layout Shift",
            "score": 0.98,
            "displayValue": "0.02",
            "numericValue": 0.02,
        },
        "speed-index": {
            "title": "Speed Index",
            "score": 0.92,
            "displayValue": "1.5 s",
            "numericValue": 1500.0,
        },
        "unused-javascript": {
            "title": "Reduce unused JavaScript",
            "score": 0.65,
            "displayValue": "Potential savings of 150 KiB",
            "description": "Reduce unused JavaScript and defer loading scripts until required.",
            "details": {"type": "opportunity"},
        },
        "color-contrast": {
            "title": "Background and foreground colors do not have sufficient contrast ratio",
            "score": 0.0,
            "displayValue": "3 elements failing",
            "description": "Low-contrast text is difficult or impossible for many users to read.",
        },
    },
}


def main():
    print("=" * 70)
    print(" Lighthouse CI Connector for Cognee - Ingestion & Formatting Demo")
    print("=" * 70)

    # 1. Create temporary LHR report
    with tempfile.TemporaryDirectory() as tmp_dir:
        report_path = Path(tmp_dir) / "lhr-checkout.json"
        report_path.write_text(json.dumps(SAMPLE_LHR, indent=2), encoding="utf-8")

        print("\n1. Parsing and validating Lighthouse CI report...")
        parsed = parse_lighthouse_report(report_path)
        print(f"   ✓ Audited URL (Sanitized): {parsed['clean_url']}")
        print(f"   ✓ Engine Version: {parsed['lighthouse_version']}")
        print(f"   ✓ Performance Score: {parsed['categories']['performance']['score']} / 100")
        print(f"   ✓ Accessibility Score: {parsed['categories']['accessibility']['score']} / 100")
        print(f"   ✓ Actionable Findings: {len(parsed['findings'])}")

        # 2. Render markdown document
        markdown_doc = format_lighthouse_report_markdown(
            parsed, build_id="ci-build-841", commit_sha="b8f1a23"
        )
        print("\n2. Rendered Document Output Preview:")
        print("-" * 50)
        print(markdown_doc)
        print("-" * 50)

        # 3. Create dlt source
        try:
            print("\n3. Initializing DLT source...")
            source = lighthouse_ci_source(reports_dir=tmp_dir, build_id="ci-build-841")
            print("   ✓ DLT source initialized successfully!")
            print(f"   ✓ Document mode marker: {getattr(source, 'cognee_document_source', None)}")
        except ImportError as e:
            print(f"   ℹ️  dlt extra skipped in minimal environment: {e}")

    print("\n✅ Lighthouse CI connector demo completed successfully!")


if __name__ == "__main__":
    main()
