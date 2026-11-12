"""Runnable example for the IETF RFC and Internet Standards connector.

Demonstrates querying RFC specifications, evaluating supersession state
(current standard vs obsoleted), inspecting relationships, and generating Cognee documents.
"""

from cognee_community_connector_ietf_rfc import (
    format_rfc_markdown,
    ietf_rfc_source,
    normalize_rfc_number,
    parse_rfc_relationships,
)

SAMPLE_RFC_9111 = {
    "number": 9111,
    "title": "HTTP Caching",
    "status": "Internet Standard",
    "stream": "IETF",
    "working_group": "httpbis",
    "publication_date": "2022-06",
    "authors": ["Roy T. Fielding", "Mark Nottingham", "Julian Reschke"],
    "also_known_as": ["STD 98"],
    "obsoletes": [7234, 2616],
    "obsoleted_by": [],
    "abstract": "Defines HTTP caching mechanisms and constraints on caching proxies and user agents.",
    "errata": [
        {
            "errata_id": 7012,
            "status": "Verified",
            "type": "Editorial",
            "section": "3.2",
            "notes": "Typographical clarification in heuristic freshness calculation.",
        }
    ],
}

SAMPLE_RFC_7234 = {
    "number": 7234,
    "title": "Hypertext Transfer Protocol (HTTP/1.1): Caching",
    "status": "Proposed Standard",
    "stream": "IETF",
    "working_group": "httpbis",
    "publication_date": "2014-06",
    "authors": ["Roy T. Fielding", "Mark Nottingham", "Julian Reschke"],
    "obsoletes": [2616],
    "obsoleted_by": [9111],
    "abstract": "Predecessor HTTP/1.1 caching specification (obsoleted by RFC 9111).",
}


def main():
    print("=" * 70)
    print(" IETF RFC & Internet Standards Connector for Cognee - Demo")
    print("=" * 70)

    # 1. Test normalization
    test_inputs = [9111, "RFC 9111", "rfc7234", "  RFC  2616 "]
    print("\n1. Normalizing RFC identifiers to canonical integers:")
    for inp in test_inputs:
        norm = normalize_rfc_number(inp)
        print(f"   - {str(inp).ljust(16)} -> {norm}")

    # 2. Test relationship parsing
    raw_rel = "RFC 7234, RFC 2616, RFC 2068"
    parsed_rel = parse_rfc_relationships(raw_rel)
    print(f"\n2. Extracted Relationship Graph from '{raw_rel}':")
    print(f"   -> {parsed_rel}")

    # 3. Format markdown preview
    print("\n3. Rendered Cognee Document Preview (RFC 9111 - Current Standard):")
    print("-" * 50)
    doc_9111 = format_rfc_markdown(SAMPLE_RFC_9111)
    print(doc_9111)
    print("-" * 50)

    print("\n4. Rendered Superseded Document Preview (RFC 7234 - Obsoleted):")
    print("-" * 50)
    doc_7234 = format_rfc_markdown(SAMPLE_RFC_7234)
    print(doc_7234)
    print("-" * 50)

    # 5. Initialize DLT source
    try:
        print("\n5. Initializing DLT source with HTTP Caching standards family...")
        source = ietf_rfc_source(snapshot_data=[SAMPLE_RFC_9111, SAMPLE_RFC_7234])
        print("   ✓ DLT source initialized successfully!")
        print(f"   ✓ Document mode marker: {getattr(source, 'cognee_document_source', None)}")
    except ImportError as e:
        print(f"   ℹ️  dlt extra skipped in minimal environment: {e}")

    print("\n✅ IETF RFC connector demo completed successfully!")


if __name__ == "__main__":
    main()
