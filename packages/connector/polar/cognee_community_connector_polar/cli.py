"""Command-Line Interface (CLI) for testing and querying the Cognee Polar Connector."""

import argparse
import os
import sys

from .polar import (
    PolarClient,
    RenderOptions,
    sync_polar_records,
)


def main():
    parser = argparse.ArgumentParser(
        description="Cognee Community Polar Connector CLI",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--token",
        default=os.getenv("POLAR_ACCESS_TOKEN", ""),
        help="Polar Organization Access Token (or POLAR_ACCESS_TOKEN env var)",
    )
    parser.add_argument(
        "--org-id",
        default=os.getenv("POLAR_ORGANIZATION_ID", ""),
        help="Optional Organization ID filter",
    )
    parser.add_argument(
        "--sandbox",
        action="store_true",
        default=os.getenv("POLAR_SANDBOX", "").lower() in ("true", "1", "yes"),
        help="Target Polar Sandbox API (https://sandbox-api.polar.sh/v1)",
    )
    parser.add_argument(
        "--include-email",
        action="store_true",
        help="Include customer email unmasked (defaults to [REDACTED] for privacy)",
    )

    args = parser.parse_args()

    token = args.token or os.getenv("POLAR_ACCESS_TOKEN")
    if not token:
        print(
            "[ERROR] Polar Access Token required. Provide via --token or POLAR_ACCESS_TOKEN.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f">> Initializing Polar Client (Sandbox={args.sandbox})...")
    client = PolarClient(access_token=token, is_sandbox=args.sandbox)
    options = RenderOptions(
        include_customer_email=args.include_email,
        include_product_description=True,
        mask_pii=not args.include_email,
    )

    print(">> Ingesting Polar resources (Customers, Subscriptions, Products, Benefits)...\n")
    count = 0
    type_counts = {}

    try:
        for doc in sync_polar_records(client, organization_id=args.org_id or None, options=options):
            count += 1
            doc_type = doc.get("type", "unknown")
            type_counts[doc_type] = type_counts.get(doc_type, 0) + 1

            print("=" * 70)
            print(f"DOCUMENT ID : {doc['id']}")
            print(f"TITLE       : {doc['title']}")
            print(f"TYPE        : {doc_type.upper()}")
            print(f"METADATA    : {doc.get('metadata')}")
            print("=" * 70)
            print(doc["text"])
            print()

        print(f"[SUCCESS] Ingested {count} Polar document(s) for Cognee AI Memory:")
        for t, cnt in type_counts.items():
            print(f"   * {t}: {cnt}")

    except Exception as exc:
        print(f"[ERROR] Sync failed: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        client.close()


if __name__ == "__main__":
    main()
