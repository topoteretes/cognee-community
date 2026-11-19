#!/usr/bin/env python3
"""Example demonstration of the Kimai time-tracking connector for cognee."""

import json
from cognee_community_connector_kimai.kimai import (
    format_customer_document,
    format_project_document,
    format_timesheet_document,
    parse_kimai_records,
    kimai_source,
)

SAMPLE_KIMAI_DATA = {
    "customers": [
        {
            "id": 1,
            "name": "Acme Innovations Corp",
            "number": "ACME-001",
            "country": "United States",
            "currency": "USD",
            "comment": "Strategic Web3 & Enterprise RAG customer.",
            "visible": True,
        }
    ],
    "projects": [
        {
            "id": 10,
            "name": "Base Network x402 Micropayment Integration",
            "number": "PRJ-BASE-402",
            "orderNumber": "ORD-2026-X402",
            "comment": "Autonomous agent micropayment settlement over Base Mainnet.",
            "visible": True,
            "customer": {"id": 1, "name": "Acme Innovations Corp"},
        }
    ],
    "activities": [
        {
            "id": 101,
            "name": "Protocol Engineering & Hardening",
            "comment": "Designing smart contract hooks and defensive verification",
            "visible": True,
            "project": {"id": 10, "name": "Base Network x402 Micropayment Integration"},
        }
    ],
    "timesheets": [
        {
            "id": 8801,
            "begin": "2026-10-24T08:00:00",
            "end": "2026-10-24T12:00:00",
            "duration": 14400,  # 4 hours
            "description": "Implemented zero-capital autonomous revenue sentinel and multi-agent settlement monitors.",
            "billable": True,
            "tags": ["base-mainnet", "x402", "autonomous-agents", "security"],
            "user": {"id": 42, "alias": "Rizul Chauhan", "username": "rizul"},
            "project": {
                "id": 10,
                "name": "Base Network x402 Micropayment Integration",
                "customer": {"id": 1, "name": "Acme Innovations Corp"},
            },
            "activity": {"id": 101, "name": "Protocol Engineering & Hardening"},
        }
    ],
}


def main():
    print("=" * 70)
    print(" Kimai Time Tracking Connector for Cognee - Demo")
    print("=" * 70)

    # 1. Parse records
    parsed_docs = parse_kimai_records(SAMPLE_KIMAI_DATA, base_url="https://kimai.enterprise.org")
    print(f"\n1. Extracted Entities & Documents: {len(parsed_docs)}")
    for doc in parsed_docs:
        print(f"   - [{doc['type'].upper()}] ID: {doc['id']} | Node sets: {doc['node_sets']}")

    # 2. Render Document Previews
    print("\n2. Customer Document Preview:")
    print("-" * 50)
    print(format_customer_document(SAMPLE_KIMAI_DATA["customers"][0], base_url="https://kimai.enterprise.org"))
    print("-" * 50)

    print("\n3. Timesheet Work Log Preview:")
    print("-" * 50)
    print(format_timesheet_document(SAMPLE_KIMAI_DATA["timesheets"][0], base_url="https://kimai.enterprise.org"))
    print("-" * 50)

    # 3. Test DLT source initialization
    print("\n4. Initializing DLT source...")
    try:
        source = kimai_source(snapshot_data=SAMPLE_KIMAI_DATA)
        print("   ✅ DLT source successfully created with replace write-disposition.")
    except ImportError as e:
        print(f"   ℹ️  dlt extra skipped in minimal environment: {e}")

    print("\n✅ Kimai connector demo completed successfully!")


if __name__ == "__main__":
    main()
