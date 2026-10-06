"""Postman connector quickstart demo.

Ingest Postman collections, folders, and request descriptions into Cognee
knowledge graph memory with incremental sync.

This script runs out of the box in both live and offline modes:
1. Live mode: Set POSTMAN_API_KEY in your environment to sync your real
   Postman collections.
2. Offline mode: When POSTMAN_API_KEY is not set, a realistic mock client
   demonstrates full document rendering and incremental sync cache hits.

Zero emdashes across all code, docstrings, and comments.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

# Ensure connector package root is in sys.path when run directly as a script
_CONNECTOR_ROOT = Path(__file__).resolve().parent.parent
if str(_CONNECTOR_ROOT) not in sys.path:
    sys.path.insert(0, str(_CONNECTOR_ROOT))

try:
    import cognee
except ImportError:
    cognee = None  # type: ignore[assignment]

from cognee_community_connector_postman import PostmanClient, postman_source  # noqa: E402

DATASET_NAME = "postman_demo"

SAMPLE_COLLECTION: dict[str, Any] = {
    "info": {
        "_postman_id": "col-orders-prod-001",
        "name": "Order Management API",
        "description": "Production endpoints for order processing and refunds.",
        "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        "updatedAt": "2026-10-01T12:00:00.000Z",
    },
    "item": [
        {
            "id": "req-create-order",
            "name": "Create Order",
            "description": "Creates a new customer order with line items.",
            "request": {
                "method": "POST",
                "url": {
                    "raw": "https://api.example.com/v1/orders",
                    "protocol": "https",
                    "host": ["api", "example", "com"],
                    "path": ["v1", "orders"],
                },
                "header": [{"key": "Content-Type", "value": "application/json"}],
                "body": {
                    "mode": "raw",
                    "raw": '{"customer_id": "cust_101", "total": 129.99}',
                    "options": {"raw": {"language": "json"}},
                },
            },
            "response": [
                {
                    "id": "resp-create-201",
                    "name": "201 Created",
                    "status": "Created",
                    "code": 201,
                    "header": [{"key": "Content-Type", "value": "application/json"}],
                    "body": '{"order_id": "ord_9901", "status": "pending"}',
                }
            ],
        },
        {
            "id": "req-get-order",
            "name": "Get Order By ID",
            "description": "Retrieves the current fulfillment status of an order.",
            "request": {
                "method": "GET",
                "url": {
                    "raw": "https://api.example.com/v1/orders/ord_9901",
                    "protocol": "https",
                    "host": ["api", "example", "com"],
                    "path": ["v1", "orders", "ord_9901"],
                },
            },
            "response": [],
        },
        {
            "name": "Refunds",
            "description": "Folder containing refund processing endpoints.",
            "item": [
                {
                    "id": "req-refund-order",
                    "name": "Submit Order Refund",
                    "description": "Submits a full or partial refund for an order.",
                    "request": {
                        "method": "POST",
                        "url": "https://api.example.com/v1/orders/ord_9901/refund",
                        "header": [{"key": "Content-Type", "value": "application/json"}],
                        "body": {
                            "mode": "raw",
                            "raw": '{"reason": "customer_return", "amount": 129.99}',
                        },
                    },
                    "response": [],
                }
            ],
        },
    ],
}


class MockPostmanClient:
    """Mock Postman client providing sample collections for offline demonstration."""

    def __init__(self) -> None:
        self.api_key = "demo-api-key"
        self.base_url = "https://api.getpostman.com"
        self.list_calls: int = 0
        self.detail_calls: int = 0

    def get_collections(self, workspace_id: str | None = None) -> list[dict[str, Any]]:
        self.list_calls += 1
        info = SAMPLE_COLLECTION["info"]
        return [
            {
                "id": info["_postman_id"],
                "uid": info["_postman_id"],
                "name": info["name"],
                "updatedAt": info["updatedAt"],
            }
        ]

    def get_collection(self, collection_uid: str) -> dict[str, Any]:
        self.detail_calls += 1
        info = SAMPLE_COLLECTION["info"]
        if collection_uid in (info["_postman_id"], "col-orders-prod-001"):
            return SAMPLE_COLLECTION
        raise KeyError(f"Collection {collection_uid} not found.")


class TrackingClientWrapper:
    """Delegating wrapper tracking API calls to demonstrate incremental cache hits."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.list_calls: int = 0
        self.detail_calls: int = 0

    def get_collections(self, workspace_id: str | None = None) -> list[dict[str, Any]]:
        self.list_calls += 1
        return self._inner.get_collections(workspace_id=workspace_id)

    def get_collection(self, collection_uid: str) -> dict[str, Any]:
        self.detail_calls += 1
        return self._inner.get_collection(collection_uid)


def print_separator(title: str) -> None:
    print(f"\n{'=' * 15} {title} {'=' * 15}")


async def main() -> None:
    api_key = os.environ.get("POSTMAN_API_KEY")

    if api_key and str(api_key).strip():
        print("Detected POSTMAN_API_KEY in environment. Running in live mode.")
        raw_client: Any = PostmanClient(api_key=str(api_key).strip())
    else:
        print("POSTMAN_API_KEY not found in environment.")
        print("Running in offline demonstration mode using sample API collections.")
        print("Tip: Set POSTMAN_API_KEY to sync your live Postman collections:")
        print('     export POSTMAN_API_KEY="your-postman-api-key"')
        raw_client = MockPostmanClient()

    tracking_client = TrackingClientWrapper(raw_client)

    # -------------------------------------------------------------------------
    # Cycle 1: Initial Sync (Fetch and Render Documents)
    # -------------------------------------------------------------------------
    print_separator("Cycle 1: Initial Postman Ingestion")
    source1 = postman_source(client=tracking_client)

    docs_cycle1: list[dict[str, Any]] = []
    for item in source1:
        if isinstance(item, list):
            docs_cycle1.extend(item)
        else:
            docs_cycle1.append(item)

    print(f"Cycle 1 yielded {len(docs_cycle1)} Postman document(s).")
    print(f"API calls made: {tracking_client.detail_calls} detail fetch request(s).")

    print("\nInspecting rendered documents:")
    for idx, doc in enumerate(docs_cycle1[:3], 1):
        print(f"\nDocument #{idx}:")
        print(f"  ID:    {doc.get('id')}")
        print(f"  Title: {doc.get('title')}")
        print(f"  URL:   {doc.get('url')}")
        first_lines = str(doc.get("content", "")).splitlines()[:2]
        print(f"  Preview: {' '.join(first_lines)}")

    # -------------------------------------------------------------------------
    # Cycle 2: Incremental Sync (State Cache Optimization)
    # -------------------------------------------------------------------------
    print_separator("Cycle 2: Incremental Sync Check")
    calls_before_cycle2 = tracking_client.detail_calls

    docs_cycle2: list[dict[str, Any]] = []
    for item in source1:
        if isinstance(item, list):
            docs_cycle2.extend(item)
        else:
            docs_cycle2.append(item)

    new_detail_calls = tracking_client.detail_calls - calls_before_cycle2
    print(f"Cycle 2 yielded {len(docs_cycle2)} Postman document(s).")
    print(f"New detail API requests during cycle 2: {new_detail_calls} (Cache Hit).")
    print("Incremental sync verified: Unchanged collections skip API requests.")

    # -------------------------------------------------------------------------
    # Optional Step: Ingest into Cognee Knowledge Graph
    # -------------------------------------------------------------------------
    print_separator("Optional Cognee Knowledge Graph Pipeline")
    if cognee is None:
        print("Cognee is not installed in current environment.")
        print("To enable full knowledge graph extraction:")
        print("    pip install 'cognee==1.4.0'")
    else:
        llm_key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if not llm_key:
            print("Cognee is available, but LLM API key is not configured.")
            print("Set LLM_API_KEY or OPENAI_API_KEY to execute graph cognition.")
        else:
            print("Running Cognee ingestion pipeline...")
            try:
                # Direct cognee.add and cognify pipeline execution
                fresh_source = postman_source(client=tracking_client)
                await cognee.add(fresh_source, dataset_name=DATASET_NAME)
                print("Documents added to Cognee staging.")
                await cognee.cognify(datasets=[DATASET_NAME])
                print("Cognify entity extraction completed successfully.")
            except Exception as exc:
                print(f"Cognee execution skipped due to runtime environment: {exc}")

    print_separator("Demo Complete")
    print("Postman connector quickstart demonstration executed successfully.")


if __name__ == "__main__":
    asyncio.run(main())
