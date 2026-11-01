"""Command-Line Interface (CLI) for testing and inspecting the Cognee Packagist Connector."""

import argparse
import sys

from .packagist import PackagistClient, PackagistRenderOptions, sync_packagist_records


def main():
    parser = argparse.ArgumentParser(
        description="Cognee Community Packagist / Composer Connector CLI",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--packages",
        default="monolog/monolog,swiftmailer/swiftmailer",
        help="Comma-separated list of Composer package names (e.g. 'monolog/monolog,laravel/framework')",
    )
    parser.add_argument(
        "--vendor",
        help="Vendor prefix to auto-discover packages (e.g. 'symfony' or 'guzzlehttp')",
    )
    parser.add_argument(
        "--token",
        help="Optional Private Packagist API token",
    )
    parser.add_argument(
        "--exclude-abandoned",
        action="store_true",
        help="Filter out and tombstone abandoned packages",
    )

    args = parser.parse_args()

    client = PackagistClient(token=args.token)

    target_packages = [p.strip() for p in args.packages.split(",") if p.strip()]

    if args.vendor:
        print(f">> Fetching package list for vendor '{args.vendor}'...")
        try:
            vendor_pkgs = client.list_vendor_packages(args.vendor)
            print(f">> Found {len(vendor_pkgs)} packages for vendor '{args.vendor}':")
            for p in vendor_pkgs[:10]:
                print(f"   * {p}")
                if p not in target_packages:
                    target_packages.append(p)
            if len(vendor_pkgs) > 10:
                print(f"   ... and {len(vendor_pkgs) - 10} more.")
        except Exception as exc:
            print(f"[ERROR] Failed fetching vendor packages: {exc}", file=sys.stderr)
            sys.exit(1)

    print(f">> Syncing and rendering documents for: {target_packages}...\n")
    options = PackagistRenderOptions(exclude_abandoned=args.exclude_abandoned)

    count = 0
    for doc in sync_packagist_records(client, target_packages, options=options):
        count += 1
        print("=" * 70)
        print(f"DOCUMENT ID : {doc['id']}")
        print(f"TITLE       : {doc['title']}")
        print(f"ABANDONED   : {doc.get('is_abandoned', False)} (Replacement: {doc.get('replacement_package')})")
        print(f"ADVISORIES  : {doc.get('advisory_count', 0)}")
        print(f"TOMBSTONE   : {doc.get('_deleted', False)}")
        print("=" * 70)
        print(doc["content"])
        print()

    print(f"[SUCCESS] Processed {count} Packagist package document(s) for Cognee AI Memory.")


if __name__ == "__main__":
    main()
