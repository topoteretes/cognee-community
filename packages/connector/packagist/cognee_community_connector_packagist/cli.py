"""Command-Line Interface (CLI) for testing and querying the Cognee Packagist Connector."""

import argparse
import sys

from .packagist import (
    PackagistClient,
    PackagistRenderOptions,
    extract_packages_from_composer_file,
    sync_packagist_records,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Cognee Community Packagist / Composer Connector CLI",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--packages",
        default="monolog/monolog,swiftmailer/swiftmailer",
        help="Comma-separated Composer package names (e.g. 'monolog/monolog,laravel/framework')",
    )
    parser.add_argument(
        "--vendor",
        help="Vendor prefix to auto-discover packages (e.g. 'symfony' or 'guzzlehttp')",
    )
    parser.add_argument(
        "--composer-file",
        help="Path to composer.json or composer.lock to automatically extract dependencies",
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

    args = parser.parse_args(argv)

    client = PackagistClient(token=args.token)

    target_packages = [p.strip() for p in args.packages.split(",") if p.strip()]

    if args.composer_file:
        print(f">> Extracting dependencies from composer file '{args.composer_file}'...")
        try:
            extracted = extract_packages_from_composer_file(args.composer_file)
            print(f">> Found {len(extracted)} dependencies: {', '.join(extracted)}")
            for p in extracted:
                if p not in target_packages:
                    target_packages.append(p)
        except Exception as exc:
            print(f"[ERROR] Failed extracting dependencies: {exc}", file=sys.stderr)
            sys.exit(1)

    if args.vendor:
        print(f">> Discovering packages for vendor '{args.vendor}'...")
        try:
            vendor_pkgs = client.list_vendor_packages(args.vendor)
            print(f">> Discovered {len(vendor_pkgs)} packages under '{args.vendor}':")
            for p in vendor_pkgs[:10]:
                print(f"   * {p}")
            if len(vendor_pkgs) > 10:
                print(f"   ... and {len(vendor_pkgs) - 10} more.")
            for p in vendor_pkgs:
                if p not in target_packages:
                    target_packages.append(p)
        except Exception as exc:
            print(f"[ERROR] Vendor listing failed: {exc}", file=sys.stderr)
            sys.exit(1)

    print(f">> Syncing and rendering documents for: {target_packages}...\n")
    options = PackagistRenderOptions(exclude_abandoned=args.exclude_abandoned)

    count = 0
    for doc in sync_packagist_records(client, target_packages, options=options):
        count += 1
        rep = doc.get("replacement_package")
        is_ab = doc.get("is_abandoned", False)
        print("=" * 70)
        print(f"DOCUMENT ID : {doc['id']}")
        print(f"TITLE       : {doc['title']}")
        print(f"ABANDONED   : {is_ab} (Replacement: {rep})")
        print(f"ADVISORIES  : {doc.get('advisory_count', 0)}")
        print(f"TOMBSTONE   : {doc.get('_deleted', False)}")
        print("=" * 70)
        print(doc["content"])
        print()

    print(f"[SUCCESS] Processed {count} Packagist package document(s) for Cognee AI Memory.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
