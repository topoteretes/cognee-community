"""Runnable example for the Penpot open-source design connector.

Demonstrates parsing Penpot design files, boards, UI copy / text layers,
component instances, and comment threads into structured Cognee documents.
"""

from cognee_community_connector_penpot import (
    format_comment_thread_markdown,
    format_page_document_markdown,
    parse_penpot_page_objects,
    penpot_source,
)

SAMPLE_PENPOT_FILE = {
    "id": "file-mobile-cart-42",
    "name": "Mobile Checkout Flow V2",
    "projectName": "Growth & Conversion Q4",
    "pages": [
        {
            "id": "page-main-flow",
            "name": "Checkout Screens",
            "objects": {
                "screen-1": {
                    "id": "screen-1",
                    "name": "1-Cart Review",
                    "type": "frame",
                },
                "screen-2": {
                    "id": "screen-2",
                    "name": "2-Payment Method Selection",
                    "type": "frame",
                },
                "text-cart-total": {
                    "id": "text-cart-total",
                    "name": "Total Amount Label",
                    "type": "text",
                    "text": "Subtotal: $42.00 USDC (Gas Sponsored by Base Paymaster)",
                },
                "text-cta": {
                    "id": "text-cta",
                    "name": "Checkout CTA Text",
                    "type": "text",
                    "text": "Slide to Confirm Instant On-Chain Settlement",
                },
                "btn-pay": {
                    "id": "btn-pay",
                    "name": "Primary Pay Slider",
                    "type": "component",
                    "componentName": "Controls/SwipeSliderPrimary",
                },
            },
        }
    ],
    "threads": [
        {
            "id": "thread-101",
            "boardName": "2-Payment Method Selection",
            "isResolved": True,
            "comments": [
                {
                    "ownerDisplayName": "Maya UX Lead",
                    "createdAt": "2026-10-25T11:00:00Z",
                    "text": "We should highlight gasless sponsorship so users know there are zero hidden fees.",
                },
                {
                    "ownerDisplayName": "Alex Tech Lead",
                    "createdAt": "2026-10-25T11:20:00Z",
                    "text": "Agreed. Added '(Gas Sponsored by Base Paymaster)' subtitle. Resolving discussion.",
                },
            ],
        }
    ],
}


def main():
    print("=" * 70)
    print(" Penpot Design Platform Connector for Cognee - Demo")
    print("=" * 70)

    # 1. Parse page objects
    page = SAMPLE_PENPOT_FILE["pages"][0]
    boards, text_layers, components = parse_penpot_page_objects(page)
    print("\n1. Extracted Page Hierarchy:")
    print(f"   - Boards Found:     {len(boards)} -> {[b['name'] for b in boards]}")
    print(f"   - Components Found: {len(components)} -> {components}")
    print(f"   - Text Layers:      {len(text_layers)}")

    # 2. Render Page Document Preview
    print("\n2. Rendered Page Document Preview:")
    print("-" * 50)
    page_md = format_page_document_markdown(
        file_name=SAMPLE_PENPOT_FILE["name"],
        page_name=page["name"],
        project_name=SAMPLE_PENPOT_FILE["projectName"],
        boards=boards,
        text_layers=text_layers,
        components=components,
    )
    print(page_md)
    print("-" * 50)

    # 3. Render Comment Thread Preview
    print("\n3. Rendered Discussion Thread Preview:")
    print("-" * 50)
    thread = SAMPLE_PENPOT_FILE["threads"][0]
    thread_md = format_comment_thread_markdown(SAMPLE_PENPOT_FILE["name"], thread)
    print(thread_md)
    print("-" * 50)

    # 4. Initialize DLT Source
    try:
        print("\n4. Initializing DLT source...")
        source = penpot_source(snapshot_data=[SAMPLE_PENPOT_FILE])
        print("   ✓ DLT source initialized successfully!")
        print(f"   ✓ Document mode marker: {getattr(source, 'cognee_document_source', None)}")
    except ImportError as e:
        print(f"   ℹ️  dlt extra skipped in minimal environment: {e}")

    print("\n✅ Penpot connector demo completed successfully!")


if __name__ == "__main__":
    main()
