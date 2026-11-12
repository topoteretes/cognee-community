"""Runnable example for the CISA KEV connector.

Demonstrates parsing, validating, and formatting CISA Known Exploited Vulnerabilities
for Cognee agent ingestion.
"""

import json
from pathlib import Path
import tempfile

from cognee_community_connector_cisa_kev import (
    cisa_kev_source,
    fetch_and_validate_kev_catalog,
    format_vulnerability_markdown,
)

SAMPLE_CATALOG = {
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
            "shortDescription": "Ivanti Connect Secure contains a generation of predictable numbers or identifiers vulnerability.",
            "requiredAction": "Apply mitigations in accordance with vendor instructions.",
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
            "dateAdded": "2026-10-30",
            "shortDescription": "Missing authentication for critical function allows privilege escalation.",
            "requiredAction": "Apply mitigations in accordance with vendor instructions.",
            "dueDate": "2026-11-02",
            "knownRansomwareCampaignUse": "Unknown",
            "notes": "https://nvd.nist.gov/vuln/detail/CVE-2026-111357",
            "cwes": ["CWE-306"],
        },
    ],
}


def main():
    print("=" * 70)
    print(" CISA KEV Connector for Cognee - Ingestion & Formatting Demo")
    print("=" * 70)

    # 1. Test offline catalog validation
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tmp:
        json.dump(SAMPLE_CATALOG, tmp, indent=2)
        tmp_path = Path(tmp.name)

    try:
        print("\n1. Validating local CISA KEV snapshot...")
        catalog = fetch_and_validate_kev_catalog(tmp_path)
        print(f"   ✓ Catalog Version: {catalog['catalogVersion']}")
        print(f"   ✓ Verified Entries: {len(catalog['vulnerabilities'])}")

        # 2. Format a sample document
        first_vuln = catalog["vulnerabilities"][0]
        markdown_doc = format_vulnerability_markdown(
            first_vuln, catalog_version=catalog["catalogVersion"]
        )
        print("\n2. Rendered Document Output Preview:")
        print("-" * 50)
        print(markdown_doc)
        print("-" * 50)

        # 3. Create dlt source
        try:
            print("\n3. Initializing DLT source...")
            source = cisa_kev_source(feed_url_or_path=tmp_path, known_ransomware_only=True)
            print("   ✓ DLT source initialized successfully!")
            print(f"   ✓ Document mode marker: {getattr(source, 'cognee_document_source', None)}")
        except ImportError as e:
            print(f"   ℹ️  dlt extra skipped in minimal environment: {e}")

    finally:
        if tmp_path.exists():
            tmp_path.unlink()

    print("\n✅ CISA KEV connector demo completed successfully!")


if __name__ == "__main__":
    main()
