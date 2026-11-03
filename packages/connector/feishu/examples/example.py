#!/usr/bin/env python3
"""Example demonstration of the Feishu / Lark Docs & Wiki connector for cognee."""

from cognee_community_connector_feishu.feishu import (
    convert_docx_blocks_to_markdown,
    format_feishu_document,
    parse_feishu_workspace,
    feishu_source,
)

SAMPLE_WORKSPACE = {
    "documents": [
        {
            "document_id": "doc_arch_001",
            "title": "Autonomous Multi-Agent Trading Swarm Architecture",
            "space_id": "space_quant_trading",
            "space_name": "Quant Alpha Knowledge Base",
            "author": "Rizul Chauhan",
            "updated_at": "2026-10-24 18:00:00",
            "blocks": [
                {
                    "block_type": 3,  # H1
                    "heading1": {"elements": [{"text_run": {"content": "Autonomous Quant Engine Overview"}}]},
                },
                {
                    "block_type": 2,  # Text
                    "text": {
                        "elements": [
                            {"text_run": {"content": "The "}},
                            {"text_run": {"content": "KEV • LAYA Quant Engine", "text_element_style": {"bold": True}}},
                            {"text_run": {"content": " operates with strict 1.0% max portfolio drawdown and "}},
                            {"text_run": {"content": "100% win rate", "text_element_style": {"inline_code": True}}},
                            {"text_run": {"content": " across BTC, ETH, and SOL pairs."}},
                        ]
                    },
                },
                {
                    "block_type": 11,
                    "bullet": {"elements": [{"text_run": {"content": "Zero personal cash required ($0)."}}]},
                },
                {
                    "block_type": 11,
                    "bullet": {"elements": [{"text_run": {"content": "Settlement rail: Base Mainnet USDC."}}]},
                },
                {
                    "block_type": 18,
                    "callout": {"elements": [{"text_run": {"content": "Verified production performance: +$3,487.30 profit."}}]},
                },
            ],
        }
    ]
}


def main():
    print("=" * 70)
    print(" Feishu / Lark Docs & Wiki Connector for Cognee - Demo")
    print("=" * 70)

    # 1. Parse workspace documents
    docs = parse_feishu_workspace(SAMPLE_WORKSPACE, domain="feishu")
    print(f"\n1. Extracted Documents: {len(docs)}")
    for doc in docs:
        print(f"   - ID: {doc['id']} | Node sets: {doc['node_sets']}")

    # 2. Render markdown preview
    print("\n2. Document Markdown Render Preview:")
    print("-" * 50)
    print(format_feishu_document(SAMPLE_WORKSPACE["documents"][0], domain="feishu"))
    print("-" * 50)

    # 3. DLT source preview
    print("\n3. Initializing DLT source...")
    try:
        source = feishu_source(snapshot_data=SAMPLE_WORKSPACE)
        print("   ✅ DLT source successfully created.")
    except ImportError as e:
        print(f"   ℹ️  dlt extra skipped in minimal environment: {e}")

    print("\n✅ Feishu connector demo completed successfully!")


if __name__ == "__main__":
    main()
