"""Runnable example for the AbuseIPDB threat intelligence connector.

Demonstrates parsing, private IP filtering, attack category resolution, and
formatting IP reputation dossiers for Cognee agent ingestion.
"""

from pathlib import Path
import tempfile

from cognee_community_connector_abuseipdb import (
    abuseipdb_source,
    format_ip_reputation_markdown,
    is_public_ip,
    resolve_category_names,
)

SAMPLE_IP_DOSSIER = {
    "ipAddress": "198.51.100.42",
    "isPublic": True,
    "ipVersion": 4,
    "isWhitelisted": False,
    "abuseConfidenceScore": 100,
    "countryCode": "US",
    "countryName": "United States",
    "usageType": "Data Center/Web Hosting/Transit",
    "isp": "Malicious Cloud Compute LLC",
    "domain": "badcloud.example",
    "hostnames": ["scanner-04.badcloud.example"],
    "isTor": False,
    "totalReports": 412,
    "lastReportedAt": "2026-10-25T14:22:00.000Z",
    "categoryIds": [18, 22, 14, 21],  # Brute-Force, SSH, Port Scan, Web App Attack
}


def main():
    print("=" * 70)
    print(" AbuseIPDB Threat Intelligence Connector for Cognee - Demo")
    print("=" * 70)

    # 1. Test private IP filtering
    test_ips = ["198.51.100.42", "127.0.0.1", "10.0.4.15", "192.168.1.1", "203.0.113.88"]
    print("\n1. Testing Local Private IP Filtering:")
    for ip in test_ips:
        status = "PUBLIC (Checked)" if is_public_ip(ip) else "PRIVATE / RESERVED (Filtered)"
        print(f"   - {ip.ljust(16)} -> {status}")

    # 2. Test category ID resolution
    cat_ids = SAMPLE_IP_DOSSIER["categoryIds"]
    resolved = resolve_category_names(cat_ids)
    print(f"\n2. Resolved Attack Categories for {cat_ids}:")
    for r in resolved:
        print(f"   ✓ {r}")

    # 3. Format markdown document
    markdown_doc = format_ip_reputation_markdown(SAMPLE_IP_DOSSIER)
    print("\n3. Rendered Document Output Preview:")
    print("-" * 50)
    print(markdown_doc)
    print("-" * 50)

    # 4. Initialize DLT source with sample snapshot
    try:
        print("\n4. Initializing DLT source with snapshot data...")
        source = abuseipdb_source(snapshot_data=[SAMPLE_IP_DOSSIER], min_confidence=50)
        print("   ✓ DLT source initialized successfully!")
        print(f"   ✓ Document mode marker: {getattr(source, 'cognee_document_source', None)}")
    except ImportError as e:
        print(f"   ℹ️  dlt extra skipped in minimal environment: {e}")

    print("\n✅ AbuseIPDB connector demo completed successfully!")


if __name__ == "__main__":
    main()
