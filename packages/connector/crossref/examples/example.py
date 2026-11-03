#!/usr/bin/env python3
"""Example demonstration of Crossref DOI metadata connector for cognee."""

from cognee_community_connector_crossref.crossref import (
    format_crossref_document,
    parse_crossref_works,
    crossref_source,
)

SAMPLE_CROSSREF_DATA = {
    "items": [
        {
            "DOI": "10.1145/3442188.3445922",
            "title": ["On the Dangers of Stochastic Parrots: Can Language Models Be Too Big? 🦜"],
            "container-title": ["Proceedings of the 2021 ACM Conference on Fairness, Accountability, and Transparency"],
            "author": [
                {"given": "Emily M.", "family": "Bender", "ORCID": "https://orcid.org/0000-0001-6585-8025"},
                {"given": "Timnit", "family": "Gebru"},
                {"given": "Angelina", "family": "McMillan-Major"},
                {"given": "Shmargaret", "family": "Shmitchell"},
            ],
            "issued": {"date-parts": [[2021, 3, 1]]},
            "is-referenced-by-count": 2450,
            "license": [
                {
                    "URL": "https://www.acm.org/publications/policies/author-rights",
                    "start": {"date-parts": [[2021, 3, 1]]},
                }
            ],
        }
    ]
}


def main():
    print("=" * 70)
    print(" Crossref DOI Metadata Connector for Cognee - Demo")
    print("=" * 70)

    # 1. Parse publication records
    docs = parse_crossref_works(SAMPLE_CROSSREF_DATA)
    print(f"\n1. Extracted Publications: {len(docs)}")
    for doc in docs:
        print(f"   - ID: {doc['id']} | Node sets: {doc['node_sets']}")

    # 2. Render Markdown Preview
    print("\n2. Publication Markdown Preview:")
    print("-" * 50)
    print(format_crossref_document(SAMPLE_CROSSREF_DATA["items"][0]))
    print("-" * 50)

    # 3. Initialize DLT Source
    print("\n3. Initializing DLT source...")
    try:
        source = crossref_source(snapshot_data=SAMPLE_CROSSREF_DATA)
        print("   ✅ DLT source successfully created.")
    except ImportError as e:
        print(f"   ℹ️  dlt extra skipped in minimal environment: {e}")

    print("\n✅ Crossref connector demo completed successfully!")


if __name__ == "__main__":
    main()
