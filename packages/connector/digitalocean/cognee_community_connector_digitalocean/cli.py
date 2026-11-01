"""Command-Line Interface (CLI) for testing and querying the Cognee DigitalOcean Connector."""

import argparse
import os
import sys

from .digitalocean import (
    DigitalOceanClient,
    DigitalOceanRenderOptions,
    sync_digitalocean_records,
)


def main():
    parser = argparse.ArgumentParser(
        description="Cognee Community DigitalOcean Cloud Infrastructure Connector CLI",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--token",
        default=os.getenv("DIGITALOCEAN_TOKEN", ""),
        help="DigitalOcean Personal Access Token (or DIGITALOCEAN_TOKEN env var)",
    )
    parser.add_argument(
        "--project-ids",
        help="Comma-separated project IDs to restrict synchronization",
    )
    parser.add_argument(
        "--skip-droplets",
        action="store_true",
        help="Skip ingesting Droplets",
    )
    parser.add_argument(
        "--skip-apps",
        action="store_true",
        help="Skip ingesting App Platform applications",
    )
    parser.add_argument(
        "--skip-databases",
        action="store_true",
        help="Skip ingesting Managed Databases",
    )
    parser.add_argument(
        "--skip-k8s",
        action="store_true",
        help="Skip ingesting Kubernetes clusters",
    )

    args = parser.parse_args()

    token = args.token or os.getenv("DIGITALOCEAN_TOKEN")
    if not token:
        print(
            "[ERROR] DigitalOcean token required. Provide via --token or DIGITALOCEAN_TOKEN.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(">> Initializing DigitalOcean API Client...")
    client = DigitalOceanClient(token=token)

    project_ids = (
        [p.strip() for p in args.project_ids.split(",") if p.strip()] if args.project_ids else None
    )

    options = DigitalOceanRenderOptions(
        include_droplets=not args.skip_droplets,
        include_apps=not args.skip_apps,
        include_databases=not args.skip_databases,
        include_kubernetes=not args.skip_k8s,
    )

    print(">> Ingesting DigitalOcean cloud resources (Droplets, Apps, Databases, Clusters)...\n")
    count = 0
    type_counts: dict[str, int] = {}

    try:
        for doc in sync_digitalocean_records(client, project_ids=project_ids, options=options):
            count += 1
            doc_type = doc.get("type", "unknown")
            type_counts[doc_type] = type_counts.get(doc_type, 0) + 1

            print("=" * 70)
            print(f"DOCUMENT ID : {doc['id']}")
            print(f"TITLE       : {doc['title']}")
            print(f"TYPE        : {doc_type.upper()}")
            print(f"RESOURCE ID : {doc.get('resource_id')}")
            print("=" * 70)
            print(doc["content"])
            print()

        print(f"[SUCCESS] Ingested {count} DigitalOcean document(s) for Cognee AI Memory:")
        for t, cnt in type_counts.items():
            print(f"   * {t}: {cnt}")

    except Exception as exc:
        print(f"[ERROR] Sync failed: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        client.close()


if __name__ == "__main__":
    main()
